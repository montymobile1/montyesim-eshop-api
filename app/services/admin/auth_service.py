"""Admin sign-in, token refresh and sign-out, done server-side against Supabase Auth.

The portal never talks to Supabase directly: it posts email/password here and receives the
Supabase session plus the admin's role and permissions. Only accounts whose
``app_metadata.role`` is an admin role get a session; any other account's fresh session is
revoked immediately and the request is refused with 403.
"""
import os
from typing import Callable, Optional

from gotrue.errors import AuthApiError, AuthError, AuthRetryableError, AuthUnknownError
from loguru import logger

from app.config.admin_permissions import permissions_for
from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.dependencies.admin_security import AdminUser
from app.exceptions.admin import AdminForbidden, AdminRateLimited, AdminUnauthorized, AdminUpstreamError
from app.repo.admin.login_attempt_repo import OUTCOME_ABORTED, OUTCOME_SUCCESS, LoginAttemptRepo
from app.schemas.admin.auth import AdminProfile, AdminSession

DEFAULT_LOGIN_RATE_LIMIT = "5/300"  # failed attempts / seconds, per IP and per email
AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"
RATE_LIMIT_STORE_UNAVAILABLE = "RATE_LIMIT_STORE_UNAVAILABLE"
INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"


def _parse_rate_limit(value: Optional[str]) -> tuple[int, int]:
    try:
        attempts, seconds = (value or DEFAULT_LOGIN_RATE_LIMIT).split("/")
        attempts, seconds = int(attempts), int(seconds)
        if attempts > 0 and seconds > 0:
            return attempts, seconds
    except ValueError:
        pass
    logger.warning(f"invalid ADMIN_LOGIN_RATE_LIMIT {value!r}, using {DEFAULT_LOGIN_RATE_LIMIT}")
    attempts, seconds = DEFAULT_LOGIN_RATE_LIMIT.split("/")
    return int(attempts), int(seconds)


def _is_upstream_failure(exc: AuthError) -> bool:
    """True when Supabase Auth itself failed (5xx, network, unparseable answer), as opposed to
    rejecting the credentials or token the caller sent."""
    if isinstance(exc, AuthApiError):
        return (getattr(exc, "status", 0) or 0) >= 500
    return isinstance(exc, (AuthRetryableError, AuthUnknownError))


class LoginRateLimiter:
    """Counts admin sign-ins per client IP and per email in a sliding window, in the database
    (``LoginAttemptRepo``), so every worker and instance shares the counts (TEOS-109).

    ``begin`` records the attempt before Supabase Auth is asked, so parallel requests are counted
    too; the 429 comes once ``max_failures`` attempts of the IP, or of the email, are in the window.
    An attempt that did not fail on the caller's side is taken back: ``succeeded`` clears that
    IP + email pair (a successful login resets the count), ``aborted`` only the attempt itself.
    When the store cannot be reached the login is refused with 502 (fail closed)."""

    def __init__(self, max_failures: int, window_seconds: int,
                 store_factory: Callable[[], LoginAttemptRepo] = LoginAttemptRepo):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._store_factory = store_factory
        self._store: Optional[LoginAttemptRepo] = None

    @classmethod
    def from_env(cls) -> "LoginRateLimiter":
        return cls(*_parse_rate_limit(os.getenv("ADMIN_LOGIN_RATE_LIMIT")))

    @property
    def store(self) -> LoginAttemptRepo:
        # created lazily so importing the router never opens a database client
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @staticmethod
    def ip_key(ip: Optional[str]) -> str:
        return (ip or "").strip() or "-"

    @staticmethod
    def email_key(email: str) -> str:
        return (email or "").strip().lower()

    def begin(self, ip: Optional[str], email: str) -> int:
        """The id of the recorded attempt. 429 when the IP or the email is at the limit, 502 when
        the store is unavailable."""
        ip_key = self.ip_key(ip)
        try:
            allowed, attempt_id = self.store.begin(ip_key, self.email_key(email), self.max_failures,
                                                   self.window_seconds)
            if allowed and attempt_id is None:
                raise ValueError("attempt allowed without an id")
        except Exception as exc:
            logger.error(f"admin login: rate limit store unavailable: {type(exc).__name__}")
            raise AdminUpstreamError(RATE_LIMIT_STORE_UNAVAILABLE) from exc
        if not allowed:
            logger.info(f"admin login refused by the rate limit: ip={ip_key}")
            raise AdminRateLimited(ErrorMessages.ADMIN_TOO_MANY_ATTEMPTS.value)
        return attempt_id

    def succeeded(self, attempt_id: int) -> None:
        self._finish(attempt_id, OUTCOME_SUCCESS)

    def aborted(self, attempt_id: int) -> None:
        self._finish(attempt_id, OUTCOME_ABORTED)

    def _finish(self, attempt_id: int, outcome: str) -> None:
        """Best effort: on failure the attempt stays counted until it leaves the window."""
        try:
            self.store.finish(attempt_id, outcome)
        except Exception as exc:
            logger.error(f"admin login: could not mark attempt {attempt_id} {outcome}: {type(exc).__name__}")


class AdminAuthService:
    def __init__(self, client_factory: Callable = supabase_client, rate_limiter: Optional[LoginRateLimiter] = None):
        self._client_factory = client_factory
        self._rate_limiter = rate_limiter or LoginRateLimiter.from_env()

    def login(self, email: str, password: str, ip: Optional[str]) -> AdminSession:
        email = email.strip()
        # recorded before Supabase is asked; it stays counted unless the credentials were accepted
        attempt_id = self._rate_limiter.begin(ip, email)
        client = self._client_factory()
        try:
            response = client.auth.sign_in_with_password({"email": email, "password": password})
        except AuthError as exc:
            if _is_upstream_failure(exc):
                logger.error(f"admin login: Supabase Auth failed: {type(exc).__name__}")
                self._rate_limiter.aborted(attempt_id)
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            raise self._login_failed()
        except Exception:
            self._rate_limiter.aborted(attempt_id)
            raise
        if not response or not response.session or not response.user:
            raise self._login_failed()
        self._rate_limiter.succeeded(attempt_id)
        return self._admin_session(client, response.session, response.user)

    def refresh(self, refresh_token: str) -> AdminSession:
        client = self._client_factory()
        try:
            response = client.auth.refresh_session(refresh_token)
        except AuthError as exc:
            if _is_upstream_failure(exc):
                logger.error(f"admin refresh: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            raise AdminUnauthorized(INVALID_REFRESH_TOKEN)
        if not response or not response.session or not response.user:
            raise AdminUnauthorized(INVALID_REFRESH_TOKEN)
        return self._admin_session(client, response.session, response.user)

    def logout(self, user: AdminUser) -> None:
        """Ends the caller's session only ("local"), so the same account stays signed in elsewhere."""
        client = self._client_factory()
        try:
            client.auth.admin.sign_out(user.token, "local")
        except AuthError as exc:
            if _is_upstream_failure(exc):
                logger.error(f"admin logout: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            logger.info("admin logout: session already ended")

    @staticmethod
    def me(user: AdminUser) -> AdminProfile:
        return AdminProfile(id=user.id, email=user.email, role=user.role, permissions=sorted(user.permissions))

    @staticmethod
    def _login_failed() -> AdminUnauthorized:
        return AdminUnauthorized(ErrorMessages.ADMIN_LOGIN_FAILED.value, name=ErrorMessages.ADMIN_LOGIN_FAILED)

    def _admin_session(self, client, session, user) -> AdminSession:
        role = (user.app_metadata or {}).get("role")
        permissions = permissions_for(role)
        if not permissions:
            self._revoke(client, session.access_token)
            raise AdminForbidden(ErrorMessages.ADMIN_ROLE_REQUIRED.value, name=ErrorMessages.ADMIN_ROLE_REQUIRED)
        return AdminSession(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            expires_in=int(session.expires_in or 0),
            user=AdminProfile(id=str(user.id), email=user.email, role=role, permissions=sorted(permissions)),
        )

    @staticmethod
    def _revoke(client, access_token: str) -> None:
        try:
            client.auth.admin.sign_out(access_token, "local")
        except Exception as exc:  # the 403 must still be returned; the session expires on its own
            logger.error(f"could not revoke non-admin session: {type(exc).__name__}")
