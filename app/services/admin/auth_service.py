"""Admin sign-in, token refresh and sign-out, done server-side against Supabase Auth.

The portal never talks to Supabase directly: it posts email/password here and receives the
Supabase session plus the admin's role and permissions. Only accounts whose
``app_metadata.role`` is an admin role get a session; any other account's fresh session is
revoked immediately and the request is refused with 403.

TEOS-307 adds the admin's own password change (current password checked, every other session signed
out) and the last step of a password reset sent from the Admins page (the reset link's session sets
the new password, then every session of the account is signed out).

TEOS-255 trusted devices (adminDocs ``TEOS-255-256-api-contract.md``): after the password, a browser that is
not a trusted device of the admin gets no session but an email code (``DeviceChallengeService``); the session
comes from ``verify_device``. Every portal session is tied to an ``admin_devices`` row (``TrustedDeviceService``):
the refresh must come from that browser, and the password actions revoke the other devices.
"""
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple, Union

from gotrue.errors import AuthApiError, AuthError, AuthRetryableError, AuthUnknownError
from loguru import logger

from app.config.admin_permissions import permissions_for
from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.dependencies.admin_security import (AdminUser, audit_refused_session, device_session_error, is_email_link_token,
                                             is_recovery_session, token_iat, token_session_id)
from app.exceptions.admin import (AdminError, AdminForbidden, AdminRateLimited, AdminUnauthorized,
                                  AdminUpstreamError, AdminValidationError, coded)
from app.repo.admin.base import AuditContext
from app.repo.admin.login_attempt_repo import OUTCOME_ABORTED, OUTCOME_SUCCESS, LoginAttemptRepo
from app.schemas.admin.auth import AdminPasswordChanged, AdminPasswordReset, AdminProfile, AdminSession
from app.schemas.admin.trusted_devices import AdminCodeResent, AdminDeviceInfo, AdminLoginChallenge
from app.services.admin.device_challenge_service import DeviceChallengeService
from app.services.admin.device_hash import device_hash
from app.services.admin.trusted_device_service import REASON_PASSWORD_CHANGE, REASON_PASSWORD_RESET

DEFAULT_LOGIN_RATE_LIMIT = "5/300"  # failed attempts / seconds, per IP and per email
AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"
RATE_LIMIT_STORE_UNAVAILABLE = "RATE_LIMIT_STORE_UNAVAILABLE"
INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"
# TEOS-307 machine codes (developerMessage)
CURRENT_PASSWORD_INVALID = "current_password_invalid"
SAME_PASSWORD = "same_password"
WEAK_PASSWORD = "weak_password"
RESET_LINK_INVALID = "reset_link_invalid"
PASSWORD_UPDATE_FAILED = "PASSWORD_UPDATE_FAILED"
#: audit rows of password actions: table ``auth.users``, operation UPDATE, ``action`` in the details
AUTH_USERS_TABLE = "auth.users"
PASSWORD_CHANGE_STEP = "password change"  # log label of the trusted-devices updates after a password change
OPERATION_UPDATE = "UPDATE"
ACTION_PASSWORD_CHANGE = "password_change"
ACTION_PASSWORD_RESET = "password_reset"
# TEOS-255
SESSION_ID_MISSING = "SESSION_ID_MISSING"
DEVICE_LOOKUP_FAILED = "DEVICE_LOOKUP_FAILED"


def _default_session_revoker(user_id: str) -> Dict[str, int]:
    """Every session and refresh token of the account deleted (TEOS-149 ``admin_revoke_user_sessions``)."""
    from app.repo.admin.user_repo import UserAdminRepo
    from app.services.admin.user_service import revoke_user_sessions
    return revoke_user_sessions(UserAdminRepo(), user_id)


def _default_audit():
    from app.services.admin.audit_service import AuditService
    return AuditService()


def _default_device_service():
    from app.services.admin.trusted_device_service import TrustedDeviceService
    return TrustedDeviceService()


def password_update_error(exc: AuthError) -> AdminError:
    """The admin error for a refused ``update_user_by_id(password)``: Supabase's own password rules (same
    password, weak or leaked password) -> 422 with a clear message; anything else -> 502."""
    code = getattr(exc, "code", None)
    if code == SAME_PASSWORD:
        return AdminValidationError(SAME_PASSWORD, name=ErrorMessages.ADMIN_PASSWORD_REUSED)
    if code == WEAK_PASSWORD:
        return AdminValidationError(WEAK_PASSWORD, name=ErrorMessages.ADMIN_PASSWORD_WEAK)
    if _is_upstream_failure(exc):
        logger.error(f"admin password: Supabase Auth failed: {type(exc).__name__}")
        return AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
    logger.error(f"admin password: update refused: {type(exc).__name__} {code}")
    return AdminUpstreamError(PASSWORD_UPDATE_FAILED)


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
    def __init__(self, client_factory: Callable = supabase_client, rate_limiter: Optional[LoginRateLimiter] = None,
                 session_revoker: Callable[[str], Dict[str, int]] = _default_session_revoker,
                 audit_factory: Callable = _default_audit,
                 device_service_factory: Callable = _default_device_service,
                 challenge_service: Optional[DeviceChallengeService] = None):
        self._client_factory = client_factory
        self._rate_limiter = rate_limiter or LoginRateLimiter.from_env()
        self._session_revoker = session_revoker
        self._audit_factory = audit_factory
        self._device_service_factory = device_service_factory
        self._device_service = None
        self.challenges = challenge_service or DeviceChallengeService(client_factory=client_factory,
                                                                      audit_factory=audit_factory)

    @property
    def devices(self):
        """The ``TrustedDeviceService``, created lazily so importing the router opens no database client."""
        if self._device_service is None:
            self._device_service = self._device_service_factory()
        return self._device_service

    def login(self, email: str, password: str, ip: Optional[str], device: Optional[AdminDeviceInfo] = None,
              user_agent: Optional[str] = None) -> Union[AdminSession, AdminLoginChallenge]:
        """Password check (rate limited, unchanged), role check (403), then TEOS-255: a trusted device of the
        admin gets the session; any other browser gets no session but an email code (``AdminLoginChallenge``)."""
        if device is None:
            raise AdminValidationError("device: Field required")
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
        session, user = response.session, response.user
        role, permissions = self._require_admin_role(client, session, user)
        admin_id = str(user.id)
        try:
            trusted = self.devices.find_trusted(admin_id, device_hash(device))
            if trusted:
                session_id = self._session_id(session.access_token)
                row = self.devices.signed_in_trusted(trusted, session_id, ip, device) or trusted
                self.devices.record_token(str(row["id"]), token_iat(session.access_token))
            else:
                row = None
        except Exception:
            self._revoke(client, session.access_token)  # never hand out a session without its device row
            raise
        if row is not None:  # signed_in_trusted writes the device_signed_in audit row
            return self._session_model(session, user, role, permissions, device_id=str(row["id"]))
        # not a trusted device: the password session is never handed out, the email code decides
        self._revoke(client, session.access_token)
        return self.challenges.start(admin_id, user.email or email, device, ip, user_agent)

    def verify_device(self, challenge_id: str, code: str, trust_device: bool, device_name: Optional[str],
                      device: AdminDeviceInfo, ip: Optional[str]) -> AdminSession:
        """TEOS-255: the email code of a login challenge. Errors: see ``DeviceChallengeService.verify``; 403 when
        the account lost its admin role meanwhile. The device row is created / updated (trusted 30 days when
        ``trust_device``) and holds the new session."""
        verified = self.challenges.verify(challenge_id, code, device, ip)
        client = self._client_factory()
        session, user = verified.session, verified.user
        role, permissions = self._require_admin_role(client, session, user)
        admin_id = str(user.id)
        try:
            session_id = self._session_id(session.access_token)
            row = self.devices.register_verified(admin_id, user.email, verified.challenge.get("device_hash")
                                                 or device_hash(device), device, session_id, ip, trust_device,
                                                 device_name, AuditContext(actor_id=admin_id, ip=ip))
            self.devices.record_token(str(row["id"]), token_iat(session.access_token))
        except Exception:
            self._revoke(client, session.access_token)
            raise
        return self._session_model(session, user, role, permissions, device_id=str(row["id"]))

    def resend_code(self, challenge_id: str, ip: Optional[str]) -> AdminCodeResent:
        """TEOS-255: a new email code for a login challenge (see ``DeviceChallengeService.resend``)."""
        return self.challenges.resend(challenge_id, ip)

    def refresh(self, refresh_token: str, device: Optional[AdminDeviceInfo] = None,
                ip: Optional[str] = None) -> AdminSession:
        """New tokens from a refresh token. TEOS-255: the refreshed session must still have an active device row
        and ``device`` must be that row's browser, else 401 (``device_revoked`` / ``device_session_required``)
        and the refreshed session is signed out."""
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
        session, user = response.session, response.user
        if is_recovery_session(session.access_token):
            # TEOS-307: a reset link's session never becomes a portal session
            raise AdminUnauthorized(INVALID_REFRESH_TOKEN)
        role, permissions = self._require_admin_role(client, session, user)
        row = self._session_device(token_session_id(session.access_token))
        error = device_session_error(row, str(user.id))
        if error is None and (device is None or device_hash(device) != row.get("device_hash")):
            logger.info("admin refresh refused: another browser than the session's device")
            error = coded(AdminUnauthorized, "device_session_required", ErrorMessages.ADMIN_DEVICE_SESSION_REQUIRED)
        if error is not None:
            audit_refused_session(row, str(user.id), ip)
            self._revoke(client, session.access_token)
            raise error
        try:  # the API's own new token: newer tokens of this session are refused (refreshed outside the API)
            self.devices.record_token(str(row["id"]), token_iat(session.access_token))
        except Exception:
            self._revoke(client, session.access_token)
            raise
        self.devices.touch(str(row["id"]), ip)  # last use for the Devices page; never raises
        return self._session_model(session, user, role, permissions, device_id=str(row["id"]))

    # ---------- TEOS-307: passwords ----------

    def change_password(self, user: AdminUser, current_password: str, new_password: str, ip: Optional[str],
                        ctx: AuditContext) -> AdminPasswordChanged:
        """The signed-in admin's own password change.

        The current password is checked with a fresh sign-in, counted by the login rate limit like a login
        (429 once the IP or the email is at the limit). 400 ``current_password_invalid`` when it is wrong
        (not 401: the portal must not take it for an expired session); 422 ``same_password`` when the new
        password is the current one; 422 ``weak_password`` when Supabase refuses it. Then the password is
        set and every OTHER session of the account is signed out, the caller's old one included: the
        caller gets the session of the check, already valid with the new password."""
        if new_password == current_password:
            raise AdminValidationError(SAME_PASSWORD, name=ErrorMessages.ADMIN_PASSWORD_REUSED)
        email = (user.email or "").strip()
        attempt_id = self._rate_limiter.begin(ip, email)
        check_client = self._client_factory()
        try:
            response = check_client.auth.sign_in_with_password({"email": email, "password": current_password})
        except AuthError as exc:
            if _is_upstream_failure(exc):
                logger.error(f"admin change password: Supabase Auth failed: {type(exc).__name__}")
                self._rate_limiter.aborted(attempt_id)
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            raise self._current_password_invalid()
        except Exception:
            self._rate_limiter.aborted(attempt_id)
            raise
        if (not response or not response.session or not response.user
                or str(response.user.id).lower() != str(user.id).lower()):
            raise self._current_password_invalid()
        self._rate_limiter.succeeded(attempt_id)
        session = response.session
        # a client that signed in sends the user's token on its admin calls: use a fresh one
        client = self._client_factory()
        try:
            client.auth.admin.update_user_by_id(str(user.id), {"password": new_password})
        except AuthError as exc:
            self._revoke(client, session.access_token)  # the check session is never handed out
            raise password_update_error(exc)
        others_signed_out = self._sign_out_others(client, session.access_token)
        self._audit_password(ctx, user.id, user.email, ACTION_PASSWORD_CHANGE,
                             other_sessions_signed_out=others_signed_out)
        self._devices_after_password_change(user, session.access_token, ctx)
        new_session = self._admin_session(client, session, response.user)
        new_session.device_id = user.device_id
        return AdminPasswordChanged(**new_session.model_dump(), other_sessions_signed_out=others_signed_out)

    def complete_password_reset(self, access_token: str, new_password: str, ip: Optional[str]) -> AdminPasswordReset:
        """Last step of a reset sent from the Admins page: ``access_token`` is the session of the reset email
        link. 400 ``reset_link_invalid`` when it is not an email link session or it is invalid, expired or
        already used; 403 when the account is no longer an admin; 422 ``same_password`` / ``weak_password``.
        Then every session of the account is signed out, the link's session included, so the reset link
        works once and the admin signs in again with the new password."""
        if not is_email_link_token(access_token):
            raise self._reset_link_invalid()
        if self._session_device(token_session_id(access_token)) is not None:
            # TEOS-255: a portal session (verify-device, amr otp / magiclink) has a device row, active or revoked;
            # a reset link's session never has one. Without this a signed-in admin could set a new password
            # without the current one.
            logger.info("admin password reset refused: the token is a portal session")
            raise self._reset_link_invalid()
        client = self._client_factory()
        try:
            response = client.auth.get_user(jwt=access_token)
        except AuthError as exc:
            if _is_upstream_failure(exc):
                logger.error(f"admin password reset: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            raise self._reset_link_invalid()
        user = getattr(response, "user", None)
        if user is None:
            raise self._reset_link_invalid()
        if not permissions_for((user.app_metadata or {}).get("role")):
            raise AdminForbidden(ErrorMessages.ADMIN_ROLE_REQUIRED.value, name=ErrorMessages.ADMIN_ROLE_REQUIRED)
        user_id = str(user.id)
        try:
            client.auth.admin.update_user_by_id(user_id, {"password": new_password})
        except AuthError as exc:
            raise password_update_error(exc)
        signed_out = self._sign_out_everywhere(user_id)
        ctx = AuditContext(actor_id=user_id, ip=ip)
        self._audit_password(ctx, user_id, user.email, ACTION_PASSWORD_RESET, sessions_signed_out=signed_out)
        self._devices_call("password reset", lambda: self.devices.revoke_all(user_id, REASON_PASSWORD_RESET, ctx))
        return AdminPasswordReset(sessions_signed_out=signed_out)

    def _devices_after_password_change(self, user: AdminUser, new_access_token: str, ctx: AuditContext) -> None:
        """TEOS-255 (D10): every OTHER device is revoked; the caller's row moves to the new session and stays
        trusted. The password is already changed, so a failure here is logged, never raised."""
        self._devices_call(PASSWORD_CHANGE_STEP, lambda: self.devices.revoke_all(
            str(user.id), REASON_PASSWORD_CHANGE, ctx, except_device_id=user.device_id))
        new_session_id = token_session_id(new_access_token)
        if user.device_id and new_session_id:
            self._devices_call(PASSWORD_CHANGE_STEP, lambda: self.devices.move_session(user.device_id, new_session_id))
            self._devices_call(PASSWORD_CHANGE_STEP, lambda: self.devices.record_token(
                user.device_id, token_iat(new_access_token)))

    @staticmethod
    def _devices_call(what: str, call: Callable[[], Any]) -> None:
        try:
            call()
        except Exception as exc:
            logger.error(f"admin {what}: trusted devices update failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _sign_out_others(client, access_token: str) -> bool:
        try:
            client.auth.admin.sign_out(access_token, "others")
            return True
        except Exception as exc:
            logger.error(f"admin change password: could not sign out the other sessions: {type(exc).__name__}")
            return False

    def _sign_out_everywhere(self, user_id: str) -> bool:
        try:
            deleted = self._session_revoker(user_id)
            logger.info(f"admin password reset: sessions of {user_id} revoked: {deleted}")
            return True
        except Exception as exc:
            logger.error(f"admin password reset: could not revoke the sessions of {user_id}: {type(exc).__name__}")
            return False

    def _audit_password(self, ctx: AuditContext, user_id: str, email: Optional[str], action: str, **details) -> None:
        """One audit row. The password is already changed and cannot be put back, so a failed audit insert
        is logged, not raised."""
        try:
            self._audit_factory().log(ctx, AUTH_USERS_TABLE, OPERATION_UPDATE, record_id=user_id, rows_affected=1,
                                      action=action, user_id=str(user_id), user_email=email,
                                      changed_at=datetime.now(timezone.utc).isoformat(), **details)
        except Exception as exc:
            logger.error(f"admin {action}: audit insert failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _current_password_invalid() -> AdminError:
        return AdminError(CURRENT_PASSWORD_INVALID, name=ErrorMessages.ADMIN_CURRENT_PASSWORD_INVALID)

    @staticmethod
    def _reset_link_invalid() -> AdminError:
        return AdminError(RESET_LINK_INVALID, name=ErrorMessages.ADMIN_RESET_LINK_INVALID)

    def logout(self, user: AdminUser) -> None:
        """Ends the caller's session only ("local"), so the same account stays signed in elsewhere. TEOS-255: the
        device row keeps its trust but no longer holds a session."""
        client = self._client_factory()
        try:
            client.auth.admin.sign_out(user.token, "local")
        except AuthError as exc:
            if _is_upstream_failure(exc):
                logger.error(f"admin logout: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            logger.info("admin logout: session already ended")
        if user.session_id:
            self._devices_call("logout", lambda: self.devices.clear_session(user.session_id))

    @staticmethod
    def me(user: AdminUser) -> AdminProfile:
        return AdminProfile(id=user.id, email=user.email, role=user.role, permissions=sorted(user.permissions))

    @staticmethod
    def _login_failed() -> AdminUnauthorized:
        return AdminUnauthorized(ErrorMessages.ADMIN_LOGIN_FAILED.value, name=ErrorMessages.ADMIN_LOGIN_FAILED)

    def _admin_session(self, client, session, user) -> AdminSession:
        role, permissions = self._require_admin_role(client, session, user)
        return self._session_model(session, user, role, permissions)

    def _require_admin_role(self, client, session, user) -> Tuple[str, frozenset]:
        """The account's admin role and permissions; 403 (and the fresh session signed out) when it has none."""
        role = (user.app_metadata or {}).get("role")
        permissions = permissions_for(role)
        if not permissions:
            self._revoke(client, session.access_token)
            raise AdminForbidden(ErrorMessages.ADMIN_ROLE_REQUIRED.value, name=ErrorMessages.ADMIN_ROLE_REQUIRED)
        return role, permissions

    @staticmethod
    def _session_model(session, user, role: str, permissions, device_id: Optional[str] = None) -> AdminSession:
        return AdminSession(
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            expires_in=int(session.expires_in or 0),
            user=AdminProfile(id=str(user.id), email=user.email, role=role, permissions=sorted(permissions)),
            device_id=device_id,
        )

    @staticmethod
    def _session_id(access_token: str) -> str:
        session_id = token_session_id(access_token)
        if not session_id:
            logger.error("admin sign-in: the Supabase access token has no session_id claim")
            raise AdminUpstreamError(SESSION_ID_MISSING)
        return session_id

    def _session_device(self, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """The device row of a session (``TrustedDeviceService.session_device``); 502 when the store fails."""
        if not session_id:
            return None
        try:
            return self.devices.session_device(session_id)
        except AdminError:
            raise
        except Exception as exc:
            logger.error(f"admin device lookup failed: {type(exc).__name__}")
            raise AdminUpstreamError(DEVICE_LOOKUP_FAILED)

    @staticmethod
    def _revoke(client, access_token: str) -> None:
        try:
            client.auth.admin.sign_out(access_token, "local")
        except Exception as exc:  # the error must still be returned; the session expires on its own
            logger.error(f"could not sign out the session: {type(exc).__name__}")
