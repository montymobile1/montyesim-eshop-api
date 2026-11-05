"""The email-code step of an admin sign-in from a browser that is not a trusted device (TEOS-255).

The code itself is created, emailed and checked by Supabase Auth (``sign_in_with_otp`` with
``should_create_user: False``, then ``verify_otp(type="email")``): it never passes through our tables,
logs, responses or audit rows. The ``admin_device_verification`` row adds the story's rules:

- the code is valid 10 minutes (``expires_at``), 5 wrong codes kill the challenge;
- 3 resends at most, 60 seconds apart; a resend restarts the 10 minutes (Supabase keeps only the last code);
- at most 5 codes per admin per hour (challenges + resends): over it, no email is sent (429 ``code_limit``);
- the verify must come from the browser that signed in (same device hash);
- two verifies at once: the atomic ``claim`` lets one through, the other gets 410 ``challenge_closed``;
- a new challenge kills the admin's other open ones: Supabase keeps ONE code per user, so several open
  challenges would each give 5 guesses at the same latest code.

Accepted limit (decision D3, shared Supabase per-user OTP): the customer sign-in uses the same Supabase email
OTP, so a customer-app login with the admin's email replaces the admin's pending code (the admin then asks for a
new one). The two flows cannot hold two live codes for one email.

Contract: adminDocs ``TEOS-255-256-api-contract.md`` sections 2 and 3.
"""
import math
import os
import re
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
from app.repo.admin.trusted_device_repo import AdminDeviceChallengeRepo, iso, utc_now
from app.schemas.admin.trusted_devices import AdminCodeResent, AdminDeviceInfo, AdminLoginChallenge
from app.services.admin.device_hash import device_hash, display_info

CODE_TTL_SECONDS = 600
RESEND_INTERVAL_SECONDS = 60
MAX_RESENDS = 3
MAX_ATTEMPTS = 5
MAX_CODES_PER_HOUR = 5
HOUR_SECONDS = 3600
DEFAULT_CODE_LENGTH = 6
AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"
#: audit rows of the sign-in step: table ``admin_devices``, ``action`` in the details (never the code)
AUDIT_TABLE = "admin_devices"
ACTION_CODE_SENT = "code_sent"
ACTION_CODE_RESENT = "code_resent"
ACTION_CODE_FAILED = "code_failed"

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def code_length_from_env() -> int:
    """``ADMIN_DEVICE_CODE_LENGTH`` (6 to 10, the range Supabase allows), default 6. It must equal the
    Supabase project's email OTP length."""
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
    return coded(AdminGone, "challenge_closed", ErrorMessages.ADMIN_DEVICE_CHALLENGE_CLOSED)


def _expired() -> AdminError:
    return coded(AdminGone, "challenge_expired", ErrorMessages.ADMIN_DEVICE_CODE_EXPIRED)


def _default_audit():
    from app.services.admin.audit_service import AuditService
    return AuditService()


@dataclass
class VerifiedChallenge:
    """A correct code: the challenge row and Supabase's ``verify_otp`` answer (session + user)."""
    challenge: Dict[str, Any]
    session: Any
    user: Any


class DeviceChallengeService:
    def __init__(self, client_factory: Callable = supabase_client,
                 repo_factory: Callable[[], AdminDeviceChallengeRepo] = AdminDeviceChallengeRepo,
                 audit_factory: Callable = _default_audit, clock: Callable[[], datetime] = utc_now,
                 code_length: Optional[int] = None):
        self._client_factory = client_factory
        self._repo_factory = repo_factory
        self._repo: Optional[AdminDeviceChallengeRepo] = None
        self._audit_factory = audit_factory
        self._clock = clock
        self._code_length = code_length

    @property
    def repo(self) -> AdminDeviceChallengeRepo:
        # created lazily so importing the router never opens a database client
        if self._repo is None:
            self._repo = self._repo_factory()
        return self._repo

    @property
    def code_length(self) -> int:
        return self._code_length or code_length_from_env()

    # ---------- login: a new challenge ----------

    def start(self, admin_id: str, email: str, device: AdminDeviceInfo, ip: Optional[str],
              user_agent: Optional[str]) -> AdminLoginChallenge:
        """A new challenge for this browser and the code emailed by Supabase. 429 ``code_limit`` (no email)
        when the admin already had 5 codes in the last hour; 503 ``email_unavailable`` when Supabase could
        not send it (the challenge is then dead)."""
        now = self._clock()
        self._fail_open_challenges(admin_id)  # also before the 429: a refused login leaves no older code usable
        self._check_hourly_limit(admin_id, now)
        challenge = self.repo.create({
            "admin_id": admin_id,
            "device_hash": device_hash(device),
            "device_info": display_info(device),
            "expires_at": iso(now + timedelta(seconds=CODE_TTL_SECONDS)),
            "last_sent_at": iso(now),
            "ip": ip,
            "user_agent": (user_agent or "")[:500] or None,
        })
        challenge_id = str(challenge["id"])
        self._send_code(challenge_id, email, on_login=True)
        self._audit(admin_id, ip, ACTION_CODE_SENT, "INSERT", challenge_id)
        return AdminLoginChallenge(challenge_id=challenge_id, destination=mask_email(email),
                                   code_length=self.code_length, expires_in=CODE_TTL_SECONDS,
                                   resend_available_in=RESEND_INTERVAL_SECONDS, resends_left=MAX_RESENDS)

    # ---------- verify ----------

    def verify(self, challenge_id: str, code: str, device: AdminDeviceInfo, ip: Optional[str]) -> VerifiedChallenge:
        """Checks, in this order: code format (422), challenge unknown (410 closed), expired (410 expired),
        consumed or dead (410 closed), another browser (400 ``device_mismatch``); then claims the challenge
        and asks Supabase. Wrong code: 400 ``code_invalid {attempts_left}``, the 5th: 429
        ``too_many_attempts``. Supabase Auth down: the claim is undone, 502."""
        length = self.code_length
        if not re.fullmatch(rf"[0-9]{{{length}}}", code or ""):
            raise coded(AdminValidationError, "code_format", ErrorMessages.ADMIN_VALIDATION_FAILED,
                        code_length=length)
        challenge = self._load_for_verify(challenge_id)
        if device_hash(device) != challenge.get("device_hash"):
            raise coded(AdminError, "device_mismatch", ErrorMessages.ADMIN_DEVICE_MISMATCH)
        admin_id = str(challenge["admin_id"])
        email = self._admin_email(admin_id)
        claimed = self.repo.claim(str(challenge["id"]))
        if claimed is None:
            raise _closed()  # lost a parallel verify, or it expired / died meanwhile
        client = self._client_factory()
        try:
            response = client.auth.verify_otp({"email": email, "token": code, "type": "email"})
        except AuthError as exc:
            if is_upstream_failure(exc):
                logger.error(f"admin verify device: Supabase Auth failed: {type(exc).__name__}")
                self._release(challenge["id"])
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            raise self._wrong_code(challenge, ip)
        except Exception:
            self._release(challenge["id"])
            raise
        session = getattr(response, "session", None)
        user = getattr(response, "user", None)
        if session is None or user is None:
            raise self._wrong_code(challenge, ip)
        if str(user.id).lower() != admin_id.lower():
            logger.error("admin verify device: the code opened a session of another account")
            self._sign_out(session.access_token)
            raise _closed()
        return VerifiedChallenge(challenge=claimed, session=session, user=user)

    def _load_for_verify(self, challenge_id: str) -> Dict[str, Any]:
        challenge = self._get(challenge_id)
        if challenge is None:
            raise _closed()
        expires_at = _as_datetime(challenge.get("expires_at"))
        if expires_at is None or expires_at <= self._clock():
            raise _expired()
        if challenge.get("consumed_at") or challenge.get("failed_at"):
            raise _closed()
        return challenge

    def _wrong_code(self, challenge: Dict[str, Any], ip: Optional[str]) -> AdminError:
        result = self.repo.failed_attempt(str(challenge["id"]), MAX_ATTEMPTS)
        if not result.get("found"):
            return _closed()
        dead = bool(result.get("dead"))
        attempts_left = int(result.get("attempts_left") or 0)
        self._audit(str(challenge["admin_id"]), ip, ACTION_CODE_FAILED, "UPDATE", str(challenge["id"]),
                    attempts=result.get("attempts"), attempts_left=attempts_left, locked=dead)
        if dead:
            return coded(AdminRateLimited, "too_many_attempts", ErrorMessages.ADMIN_DEVICE_TOO_MANY_ATTEMPTS)
        return coded(AdminError, "code_invalid", ErrorMessages.ADMIN_DEVICE_CODE_INVALID, attempts_left=attempts_left)

    def _release(self, challenge_id: Any) -> None:
        try:
            self.repo.release(str(challenge_id))
        except Exception as exc:  # the challenge then stays claimed: the admin signs in again
            logger.error(f"admin verify device: could not release the challenge: {type(exc).__name__}")

    # ---------- resend ----------

    def resend(self, challenge_id: str, ip: Optional[str]) -> AdminCodeResent:
        """A new code for the same challenge (the previous one stops working). 410 closed / expired;
        429 ``resend_limit`` (3 used), ``resend_too_soon {retry_after}`` (< 60 s), ``code_limit``
        (5 codes in the hour); 503 when Supabase could not send it (the challenge is then dead)."""
        challenge = self._get(challenge_id)
        if challenge is None or challenge.get("consumed_at") or challenge.get("failed_at"):
            raise _closed()
        now = self._clock()
        expires_at = _as_datetime(challenge.get("expires_at"))
        if expires_at is None or expires_at <= now:
            raise _expired()
        resend_count = int(challenge.get("resend_count") or 0)
        if resend_count >= MAX_RESENDS:
            raise coded(AdminRateLimited, "resend_limit", ErrorMessages.ADMIN_DEVICE_RESEND_LIMIT)
        last_sent_at = _as_datetime(challenge.get("last_sent_at")) or now
        elapsed = (now - last_sent_at).total_seconds()
        if elapsed < RESEND_INTERVAL_SECONDS:
            raise self._too_soon(max(1, math.ceil(RESEND_INTERVAL_SECONDS - elapsed)))
        admin_id = str(challenge["admin_id"])
        self._check_hourly_limit(admin_id, now)
        email = self._admin_email(admin_id)
        updated = self.repo.record_resend(str(challenge["id"]), resend_count,
                                          now + timedelta(seconds=CODE_TTL_SECONDS))
        if updated is None:  # another resend (or a verify) got there first
            raise self._too_soon(RESEND_INTERVAL_SECONDS)
        self._send_code(str(challenge["id"]), email)
        resends_left = MAX_RESENDS - (resend_count + 1)
        self._audit(admin_id, ip, ACTION_CODE_RESENT, "UPDATE", str(challenge["id"]), resends_left=resends_left)
        return AdminCodeResent(expires_in=CODE_TTL_SECONDS, resend_available_in=RESEND_INTERVAL_SECONDS,
                               resends_left=resends_left)

    @staticmethod
    def _too_soon(retry_after: int) -> AdminError:
        return coded(AdminRateLimited, "resend_too_soon", ErrorMessages.ADMIN_DEVICE_RESEND_TOO_SOON,
                     retry_after=retry_after)

    # ---------- shared ----------

    def _get(self, challenge_id: str) -> Optional[Dict[str, Any]]:
        if not _UUID.match(challenge_id or ""):
            return None  # never sent to the database (a uuid column would answer with an error)
        return self.repo.get(challenge_id)

    def _fail_open_challenges(self, admin_id: str) -> None:
        killed = self.repo.fail_open(admin_id)
        if killed:
            logger.info(f"admin device code: {killed} older open challenge(s) of {admin_id} closed")

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

    def _send_code(self, challenge_id: str, email: str, on_login: bool = False) -> None:
        """Supabase creates and emails the code. On failure the challenge is marked dead and 503 is returned.

        Supabase's own send limit (429: by default one email per user per 60 s, plus the project's hourly email
        limit) is not an outage: 429 ``resend_too_soon {retry_after: 60}``. On a resend the challenge stays alive
        (the admin presses Resend again); on a login it is closed (the admin signs in again a minute later)."""
        client = self._client_factory()
        try:
            client.auth.sign_in_with_otp({"email": email, "options": {"should_create_user": False}})
        except AuthApiError as exc:
            if (getattr(exc, "status", 0) or 0) != 429:
                self._email_failed(challenge_id, exc)
            logger.info("admin device code: Supabase email send limit reached")
            if on_login:
                self._mark_failed(challenge_id)
            raise self._too_soon(RESEND_INTERVAL_SECONDS)
        except Exception as exc:
            self._email_failed(challenge_id, exc)

    def _mark_failed(self, challenge_id: str) -> None:
        try:
            self.repo.mark_failed(challenge_id)
        except Exception as mark_exc:
            logger.error(f"admin device code: could not mark the challenge failed: {type(mark_exc).__name__}")

    def _email_failed(self, challenge_id: str, exc: Exception) -> None:
        logger.error(f"admin device code: the email could not be sent: {type(exc).__name__}")
        self._mark_failed(challenge_id)
        raise coded(AdminServiceUnavailable, "email_unavailable", ErrorMessages.ADMIN_DEVICE_EMAIL_FAILED)

    def _sign_out(self, access_token: str) -> None:
        try:
            self._client_factory().auth.admin.sign_out(access_token, "local")
        except Exception as exc:
            logger.error(f"admin verify device: could not sign the session out: {type(exc).__name__}")

    def _audit(self, admin_id: str, ip: Optional[str], action: str, operation: str, challenge_id: str,
               **details) -> None:
        """One audit row (never the code). A failed insert is logged, not raised."""
        try:
            self._audit_factory().log(AuditContext(actor_id=admin_id, ip=ip), AUDIT_TABLE, operation,
                                      record_id=challenge_id, action=action, admin_id=admin_id,
                                      challenge_id=challenge_id, **details)
        except Exception as exc:
            logger.error(f"admin device {action}: audit insert failed: {type(exc).__name__}")
