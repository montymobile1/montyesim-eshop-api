"""Admin sign-in, token refresh and sign-out, done server-side against Supabase Auth.

The portal never talks to Supabase directly: it posts email/password here and receives the
Supabase session plus the admin's role and permissions. Only accounts whose
``app_metadata.role`` is an admin role get a session; any other account's fresh session is
revoked immediately and the request is refused with 403.
"""
import os
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Optional

from gotrue.errors import AuthApiError, AuthError, AuthRetryableError, AuthUnknownError
from loguru import logger

from app.config.admin_permissions import permissions_for
from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.dependencies.admin_security import AdminUser
from app.exceptions.admin import AdminForbidden, AdminRateLimited, AdminUnauthorized, AdminUpstreamError
from app.schemas.admin.auth import AdminProfile, AdminSession

DEFAULT_LOGIN_RATE_LIMIT = "5/300"  # failed attempts / seconds, per IP + email
AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"
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
    """Counts failed sign-ins per key in a sliding window. In-process only: behind several
    replicas each replica counts separately (Supabase Auth keeps its own limits as well)."""

    def __init__(self, max_failures: int, window_seconds: int, clock: Callable[[], float] = time.monotonic):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "LoginRateLimiter":
        return cls(*_parse_rate_limit(os.getenv("ADMIN_LOGIN_RATE_LIMIT")))

    def _recent(self, key: str, now: float) -> int:
        failures = self._failures.get(key)
        if not failures:
            return 0
        while failures and now - failures[0] >= self.window_seconds:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return 0
        return len(failures)

    def check(self, key: str) -> None:
        with self._lock:
            if self._recent(key, self._clock()) >= self.max_failures:
                raise AdminRateLimited(ErrorMessages.ADMIN_TOO_MANY_ATTEMPTS.value)

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._recent(key, now)
            self._failures[key].append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class AdminAuthService:
    def __init__(self, client_factory: Callable = supabase_client, rate_limiter: Optional[LoginRateLimiter] = None):
        self._client_factory = client_factory
        self._rate_limiter = rate_limiter or LoginRateLimiter.from_env()

    def login(self, email: str, password: str, ip: Optional[str]) -> AdminSession:
        email = email.strip()
        key = f"{ip or '-'}|{email.lower()}"
        self._rate_limiter.check(key)
        client = self._client_factory()
        try:
            response = client.auth.sign_in_with_password({"email": email, "password": password})
        except AuthError as exc:
            if _is_upstream_failure(exc):
                logger.error(f"admin login: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            self._rate_limiter.record_failure(key)
            raise self._login_failed()
        if not response or not response.session or not response.user:
            self._rate_limiter.record_failure(key)
            raise self._login_failed()
        self._rate_limiter.reset(key)
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
