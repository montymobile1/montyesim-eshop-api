"""The email-code step of an admin sign-in from a browser that is not a trusted device (TEOS-255).

The code is ours, like the customer OTP (``user_otp_service``): random digits from ``secrets``, stored only as
HMAC-SHA256 (``OTP_HMAC_SECRET``) in ``admin_otp``, emailed through the API's SMTP (``admin_mailer``), never
through Supabase's sender. It never appears in logs, responses or audit rows.

The password session of the login is ended at once (it is never handed out). Only after a correct code a new
session is opened for the admin, server-side: ``generate_link`` (magic link, nothing is emailed) and its token
verified at once, as the customer phone sign-in does. No token is ever stored.

The ``admin_device_verification`` row carries the story's rules:

- the code is valid 10 minutes (``expires_at``), 5 wrong codes kill the verification;
- 3 resends at most, 60 seconds apart; a resend sends a new code (the previous one stops working) and restarts
  the 10 minutes;
- at most 5 codes per admin per hour (verifications + resends): over it, no email is sent (429 ``code_limit``);
- the verify must come from the browser that signed in (same device hash);
- two verifies at once: the atomic ``claim`` lets one through, the other gets 410 ``verification_closed``;
- a new verification kills the admin's other open ones.

No SMTP configured: 503 ``email_not_configured``, before any verification or code (only trusted browsers sign in).

Contract: adminDocs ``TEOS-255-256-api-contract.md`` sections 2 and 3.
"""
import hmac
import math
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from gotrue.errors import AuthApiError, AuthError, AuthRetryableError, AuthUnknownError
from loguru import logger

from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.exceptions.admin import (AdminError, AdminGone, AdminRateLimited, AdminServiceUnavailable,
                                  AdminUpstreamError, AdminValidationError, coded)
from app.repo.admin.base import AuditContext
from app.repo.admin.trusted_device_repo import AdminDeviceVerificationRepo, AdminOtpRepo, iso, utc_now
from app.schemas.admin.trusted_devices import AdminCodeResent, AdminDeviceInfo, AdminLoginVerification
from app.services.admin import admin_mailer
from app.services.admin.device_hash import device_hash, display_info

CODE_TTL_SECONDS = 600
RESEND_INTERVAL_SECONDS = 60
MAX_RESENDS = 3
MAX_ATTEMPTS = 5
MAX_CODES_PER_HOUR = 5
HOUR_SECONDS = 3600
DEFAULT_CODE_LENGTH = 6
AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"
#: tries to open the session after a correct code (a concurrent magic link of the same account replaces the token)
MAX_SESSION_ATTEMPTS = 2
#: audit rows of the sign-in step: table ``admin_devices``, ``action`` in the details (never the code)
AUDIT_TABLE = "admin_devices"
ACTION_CODE_SENT = "code_sent"
ACTION_CODE_RESENT = "code_resent"
ACTION_CODE_FAILED = "code_failed"

TEMPLATE_SIGN_IN_CODE = "admin_sign_in_code.htm"
SUBJECT_SIGN_IN_CODE = "Your admin portal sign-in code"

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def code_length_from_env() -> int:
    """``ADMIN_DEVICE_CODE_LENGTH`` (6 to 10), default 6: digits of the sign-in code."""
    value = (os.getenv("ADMIN_DEVICE_CODE_LENGTH") or "").strip()
    if not value:
        return DEFAULT_CODE_LENGTH
    try:
        length = int(value)
        if 6 <= length <= 10:
            return length
    except ValueError:
        pass
    logger.warning(f"invalid ADMIN_DEVICE_CODE_LENGTH {value!r}, using {DEFAULT_CODE_LENGTH}")
    return DEFAULT_CODE_LENGTH


def new_code(length: int) -> str:
    """``length`` digits from a CSPRNG, leading zeros kept (like ``UserOtpService._new_code``)."""
    return f"{secrets.randbelow(10 ** length):0{length}d}"


def hash_code(code: str) -> str:
    """The only stored form of a code: HMAC-SHA256 keyed with ``OTP_HMAC_SECRET``, as for ``user_otp``."""
    from app.services.user_otp_service import hash_otp
    return hash_otp(code)


def mask_email(email: Optional[str]) -> str:
    """``john@montymobile.com`` -> ``j***@m***.com``."""
    local, _, domain = (email or "").strip().partition("@")
    name, _, _ = domain.partition(".")
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    masked_domain = f"{name[:1]}***" + (f".{tld}" if tld else "")
    return f"{local[:1]}***@{masked_domain}"


def is_upstream_failure(exc: Exception) -> bool:
    """Supabase Auth itself failed (5xx, network, unreadable answer), as opposed to refusing the request."""
    if isinstance(exc, AuthApiError):
        return (getattr(exc, "status", 0) or 0) >= 500
    return isinstance(exc, (AuthRetryableError, AuthUnknownError))


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _closed() -> AdminError:
    return coded(AdminGone, "verification_closed", ErrorMessages.ADMIN_DEVICE_VERIFICATION_CLOSED)


def _expired() -> AdminError:
    return coded(AdminGone, "verification_expired", ErrorMessages.ADMIN_DEVICE_CODE_EXPIRED)


def email_not_configured() -> AdminError:
    """503: the client has no SMTP, so no code (or reset link) can be emailed."""
    return coded(AdminServiceUnavailable, "email_not_configured", ErrorMessages.ADMIN_EMAIL_NOT_CONFIGURED)


def _default_audit():
    from app.services.admin.audit_service import AuditService
    return AuditService()


def _default_code_sender(email: str, code: str, minutes: int) -> None:
    admin_mailer.send_now(TEMPLATE_SIGN_IN_CODE, SUBJECT_SIGN_IN_CODE, email, {"code": code, "minutes": minutes})


@dataclass
class CompletedVerification:
    """A correct code: the verification row and the admin's new Supabase session (session + user)."""
    verification: Dict[str, Any]
    session: Any
    user: Any


class DeviceVerificationService:
    def __init__(self, client_factory: Callable = supabase_client,
                 repo_factory: Callable[[], AdminDeviceVerificationRepo] = AdminDeviceVerificationRepo,
                 audit_factory: Callable = _default_audit, clock: Callable[[], datetime] = utc_now,
                 code_length: Optional[int] = None,
                 otp_repo_factory: Callable[[], AdminOtpRepo] = AdminOtpRepo,
                 code_sender: Callable[[str, str, int], None] = _default_code_sender,
                 smtp_check: Callable[[], bool] = admin_mailer.smtp_configured):
        self._client_factory = client_factory
        self._repo_factory = repo_factory
        self._repo: Optional[AdminDeviceVerificationRepo] = None
        self._otp_repo_factory = otp_repo_factory
        self._otp_repo: Optional[AdminOtpRepo] = None
        self._audit_factory = audit_factory
        self._clock = clock
        self._code_length = code_length
        self._code_sender = code_sender
        self._smtp_check = smtp_check

    @property
    def repo(self) -> AdminDeviceVerificationRepo:
        # created lazily so importing the router never opens a database client
        if self._repo is None:
            self._repo = self._repo_factory()
        return self._repo

    @property
    def otp_repo(self) -> AdminOtpRepo:
        if self._otp_repo is None:
            self._otp_repo = self._otp_repo_factory()
        return self._otp_repo

    @property
    def code_length(self) -> int:
        return self._code_length or code_length_from_env()

    # ---------- login: a new verification ----------

    def start(self, admin_id: str, email: str, device: AdminDeviceInfo, ip: Optional[str],
              user_agent: Optional[str]) -> AdminLoginVerification:
        """A new verification for this browser and its code emailed. 503 ``email_not_configured`` (no SMTP);
        429 ``code_limit`` (no email) when the admin already had 5 codes in the last hour; 503
        ``email_unavailable`` when the email could not be sent (the verification is then dead)."""
        if not self._smtp_check():
            logger.error("admin device code: SMTP is not configured, the sign-in code cannot be emailed")
            raise email_not_configured()
        now = self._clock()
        self._fail_open_verifications(admin_id)  # also before the 429: a refused login leaves no older code usable
        self._check_hourly_limit(admin_id, now)
        expires_at = now + timedelta(seconds=CODE_TTL_SECONDS)
        verification = self.repo.create({
            "admin_id": admin_id,
            "device_hash": device_hash(device),
            "device_info": display_info(device),
            "expires_at": iso(expires_at),
            "last_sent_at": iso(now),
            "ip": ip,
            "user_agent": (user_agent or "")[:500] or None,
        })
        verification_id = str(verification["id"])
        self._issue_code(verification_id, admin_id, email, expires_at)
        self._audit(admin_id, ip, ACTION_CODE_SENT, "INSERT", verification_id)
        return AdminLoginVerification(verification_id=verification_id, destination=mask_email(email),
                                   code_length=self.code_length, expires_in=CODE_TTL_SECONDS,
                                   resend_available_in=RESEND_INTERVAL_SECONDS, resends_left=MAX_RESENDS)

    # ---------- verify ----------

    def verify(self, verification_id: str, code: str, device: AdminDeviceInfo, ip: Optional[str]) -> CompletedVerification:
        """Checks, in this order: code format (422), verification unknown (410 closed), expired (410 expired),
        consumed or dead (410 closed), another browser (400 ``device_mismatch``); then claims the verification
        and compares the code with the verification's live ``admin_otp`` row. Wrong code: 400 ``code_invalid
        {attempts_left}``, the 5th: 429 ``too_many_attempts``. Correct code: the admin's new session is opened;
        Supabase Auth down meanwhile: the claim is undone (the code still works), 502."""
        length = self.code_length
        if not re.fullmatch(rf"[0-9]{{{length}}}", code or ""):
            raise coded(AdminValidationError, "code_format", ErrorMessages.ADMIN_VALIDATION_FAILED,
                        code_length=length)
        verification = self._load_for_verify(verification_id)
        if device_hash(device) != verification.get("device_hash"):
            raise coded(AdminError, "device_mismatch", ErrorMessages.ADMIN_DEVICE_MISMATCH)
        admin_id = str(verification["admin_id"])
        email = self._admin_email(admin_id)
        claimed = self.repo.claim(str(verification["id"]))
        if claimed is None:
            raise _closed()  # lost a parallel verify, or it expired / died meanwhile
        try:
            otp = self.otp_repo.active(str(verification["id"]))
        except Exception:
            self._release(verification["id"])
            raise
        if otp is None or not hmac.compare_digest(str(otp.get("otp") or ""), hash_code(code)):
            raise self._wrong_code(verification, ip)
        session, user = self._open_session(verification["id"], email)
        if str(user.id).lower() != admin_id.lower():
            logger.error("admin verify device: the new session belongs to another account")
            self._sign_out(session.access_token)
            raise _closed()
        try:
            self.otp_repo.mark_used(otp["id"])
        except Exception as exc:  # the verification is consumed: the code cannot be used again anyway
            logger.error(f"admin verify device: the code could not be marked used: {type(exc).__name__}")
        return CompletedVerification(verification=claimed, session=session, user=user)

    def _open_session(self, verification_id: Any, email: str):
        """A new session of the admin, opened server-side after the correct code: a magic link is generated
        (GoTrue sends no email for admin links) and its token verified at once on a fresh client. Any failure
        undoes the claim (the code keeps working) and answers 502."""
        for attempt in range(1, MAX_SESSION_ATTEMPTS + 1):
            try:
                link = self._client_factory().auth.admin.generate_link({"type": "magiclink", "email": email})
                properties = link.properties
                response = self._client_factory().auth.verify_otp(
                    {"token_hash": properties.hashed_token,
                     "type": getattr(properties, "verification_type", None) or "magiclink"})
            except AuthError as exc:
                if not is_upstream_failure(exc) and attempt < MAX_SESSION_ATTEMPTS:
                    logger.warning("admin verify device: the session token was replaced meanwhile, retrying")
                    continue
                logger.error(f"admin verify device: the session could not be opened: {type(exc).__name__}")
                self._release(verification_id)
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            except Exception:
                self._release(verification_id)
                raise
            session = getattr(response, "session", None)
            user = getattr(response, "user", None)
            if session is not None and user is not None:
                return session, user
            logger.error("admin verify device: Supabase answered without a session")
            break
        self._release(verification_id)
        raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)

    def _load_for_verify(self, verification_id: str) -> Dict[str, Any]:
        verification = self._get(verification_id)
        if verification is None:
            raise _closed()
        expires_at = _as_datetime(verification.get("expires_at"))
        if expires_at is None or expires_at <= self._clock():
            raise _expired()
        if verification.get("consumed_at") or verification.get("failed_at"):
            raise _closed()
        return verification

    def _wrong_code(self, verification: Dict[str, Any], ip: Optional[str]) -> AdminError:
        result = self.repo.failed_attempt(str(verification["id"]), MAX_ATTEMPTS)
        if not result.get("found"):
            return _closed()
        dead = bool(result.get("dead"))
        attempts_left = int(result.get("attempts_left") or 0)
        self._audit(str(verification["admin_id"]), ip, ACTION_CODE_FAILED, "UPDATE", str(verification["id"]),
                    attempts=result.get("attempts"), attempts_left=attempts_left, locked=dead)
        if dead:
            return coded(AdminRateLimited, "too_many_attempts", ErrorMessages.ADMIN_DEVICE_TOO_MANY_ATTEMPTS)
        return coded(AdminError, "code_invalid", ErrorMessages.ADMIN_DEVICE_CODE_INVALID, attempts_left=attempts_left)

    def _release(self, verification_id: Any) -> None:
        try:
            self.repo.release(str(verification_id))
        except Exception as exc:  # the verification then stays claimed: the admin signs in again
            logger.error(f"admin verify device: could not release the verification: {type(exc).__name__}")

    # ---------- resend ----------

    def resend(self, verification_id: str, ip: Optional[str]) -> AdminCodeResent:
        """A new code for the same verification (the previous one stops working); the 10 minutes restart.
        TEOS-256 case 14: an EXPIRED code can be resent too (the story's "Code expired" with a Resend option),
        within the same limits. 410 closed (unknown, used or dead); 429 ``resend_limit`` (3 used),
        ``resend_too_soon {retry_after}`` (< 60 s), ``code_limit`` (5 codes in the hour); 503
        ``email_not_configured`` (no SMTP, the verification stays as it is) or ``email_unavailable`` (the email
        could not be sent: the verification is then dead)."""
        verification = self._get(verification_id)
        if verification is None or verification.get("consumed_at") or verification.get("failed_at"):
            raise _closed()
        now = self._clock()
        resend_count = int(verification.get("resend_count") or 0)
        if resend_count >= MAX_RESENDS:
            raise coded(AdminRateLimited, "resend_limit", ErrorMessages.ADMIN_DEVICE_RESEND_LIMIT)
        last_sent_at = _as_datetime(verification.get("last_sent_at")) or now
        elapsed = (now - last_sent_at).total_seconds()
        if elapsed < RESEND_INTERVAL_SECONDS:
            raise self._too_soon(max(1, math.ceil(RESEND_INTERVAL_SECONDS - elapsed)))
        if not self._smtp_check():
            logger.error("admin device code: SMTP is not configured, the sign-in code cannot be emailed")
            raise email_not_configured()
        admin_id = str(verification["admin_id"])
        self._check_hourly_limit(admin_id, now)
        email = self._admin_email(admin_id)
        expires_at = now + timedelta(seconds=CODE_TTL_SECONDS)
        updated = self.repo.record_resend(str(verification["id"]), resend_count, expires_at)
        if updated is None:  # another resend (or a verify) got there first
            raise self._too_soon(RESEND_INTERVAL_SECONDS)
        self._issue_code(str(verification["id"]), admin_id, email, expires_at)
        resends_left = MAX_RESENDS - (resend_count + 1)
        self._audit(admin_id, ip, ACTION_CODE_RESENT, "UPDATE", str(verification["id"]), resends_left=resends_left)
        return AdminCodeResent(expires_in=CODE_TTL_SECONDS, resend_available_in=RESEND_INTERVAL_SECONDS,
                               resends_left=resends_left)

    @staticmethod
    def _too_soon(retry_after: int) -> AdminError:
        return coded(AdminRateLimited, "resend_too_soon", ErrorMessages.ADMIN_DEVICE_RESEND_TOO_SOON,
                     retry_after=retry_after)

    # ---------- shared ----------

    def _get(self, verification_id: str) -> Optional[Dict[str, Any]]:
        if not _UUID.match(verification_id or ""):
            return None  # never sent to the database (a uuid column would answer with an error)
        return self.repo.get(verification_id)

    def _fail_open_verifications(self, admin_id: str) -> None:
        killed = self.repo.fail_open(admin_id)
        if killed:
            logger.info(f"admin device code: {killed} older open verification(s) of {admin_id} closed")

    def _check_hourly_limit(self, admin_id: str, now: datetime) -> None:
        sent = self.repo.sends_since(admin_id, now - timedelta(seconds=HOUR_SECONDS))
        if sent >= MAX_CODES_PER_HOUR:
            logger.info(f"admin device code refused by the hourly limit: admin={admin_id}")
            # the store gives a count, not the send times: the whole window is the safe upper bound
            raise coded(AdminRateLimited, "code_limit", ErrorMessages.ADMIN_DEVICE_CODE_LIMIT,
                        retry_after=HOUR_SECONDS)

    def _admin_email(self, admin_id: str) -> str:
        client = self._client_factory()
        try:
            response = client.auth.admin.get_user_by_id(admin_id)
        except AuthError as exc:
            if is_upstream_failure(exc):
                logger.error(f"admin device code: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            raise _closed()  # the account is gone
        user = getattr(response, "user", None)
        email = getattr(user, "email", None) if user is not None else None
        if not email:
            raise _closed()
        return email

    def _issue_code(self, verification_id: str, admin_id: str, email: str, expires_at: datetime) -> None:
        """A new code for the verification: the previous live one stops working, the new one is stored hashed in
        ``admin_otp`` and emailed now. On failure the verification is marked dead and 503 is returned."""
        code = new_code(self.code_length)
        try:
            self.otp_repo.expire_active(verification_id)
            self.otp_repo.create({"admin_id": admin_id, "verification_id": verification_id, "otp": hash_code(code),
                                  "expire_at": iso(expires_at), "is_used": False})
        except Exception as exc:
            logger.error(f"admin device code: the code could not be stored: {type(exc).__name__}")
            self._mark_failed(verification_id)
            raise
        try:
            self._code_sender(email, code, CODE_TTL_SECONDS // 60)
        except Exception as exc:
            logger.error(f"admin device code: the email could not be sent: {type(exc).__name__}")
            self._mark_failed(verification_id)
            raise coded(AdminServiceUnavailable, "email_unavailable", ErrorMessages.ADMIN_DEVICE_EMAIL_FAILED)

    def _mark_failed(self, verification_id: str) -> None:
        try:
            self.repo.mark_failed(verification_id)
        except Exception as mark_exc:
            logger.error(f"admin device code: could not mark the verification failed: {type(mark_exc).__name__}")

    def _sign_out(self, access_token: str) -> None:
        try:
            self._client_factory().auth.admin.sign_out(access_token, "local")
        except Exception as exc:
            logger.error(f"admin verify device: could not sign the session out: {type(exc).__name__}")

    def _audit(self, admin_id: str, ip: Optional[str], action: str, operation: str, verification_id: str,
               **details) -> None:
        """One audit row (never the code). A failed insert is logged, not raised."""
        try:
            self._audit_factory().log(AuditContext(actor_id=admin_id, ip=ip), AUDIT_TABLE, operation,
                                      record_id=verification_id, action=action, admin_id=admin_id,
                                      verification_id=verification_id, **details)
        except Exception as exc:
            logger.error(f"admin device {action}: audit insert failed: {type(exc).__name__}")
