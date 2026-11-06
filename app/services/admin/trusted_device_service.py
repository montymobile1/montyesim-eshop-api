"""Admin trusted devices: the device rows, their sessions, revocation, the 5-device limit, emails (TEOS-255).

SKELETON written by the coordinator (step 1). The signatures and docstrings are the contract between the
sign-in work (auth_service / admin_security, agent BE-1) and the devices work (this file, agent BE-2).
BE-2 implements every method; BE-1 only calls them (and mocks them in its tests). Do not change a signature
without telling the coordinator.

Rules (story TEOS-255 + adminDocs TEOS-255-256-api-contract.md):
- trust lasts ``TRUST_DAYS`` days; at most ``MAX_TRUSTED`` trusted devices per admin: trusting one more
  revokes the least recently used trusted one (reason ``trusted_limit``) and the email names it
- revoking a row = ``AdminDeviceRepo.mark_revoked`` + ``AdminDeviceRepo.end_session(session_id)`` (the
  device's next request and refresh get 401 ``device_revoked``) + one audit row
- emails (new device signed in, device removed by the limit) go through the API mailer
  (``app.config.config.send_email``) only when SMTP is configured; otherwise skipped with a log line.
  They never fail or slow down the request (sent in the background).

Implementation notes (BE-2):
- an audit row that cannot be written is logged and the action stays done (the device change is a
  security action: undoing a revoke because the audit table is down would be worse)
- an ``end_session`` failure is logged and the row stays revoked: the per-request check refuses a
  revoked row's session anyway
- the audit rows never hold the device key, the device hash or a code
"""
import html
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from loguru import logger

from app.config.constants import ErrorMessages
from app.exceptions.admin import AdminNotFound, coded
from app.repo.admin.base import AuditContext
from app.repo.admin.trusted_device_repo import AdminDeviceRepo, iso, utc_now
from app.schemas.admin.trusted_devices import AdminDeviceInfo, AdminDeviceRow
from app.services.admin.device_hash import device_label, display_info

TRUST_DAYS = 30
MAX_TRUSTED = 5

# revoke reasons (admin_devices.revoke_reason and the audit details)
REASON_USER = "revoked_by_user"                 # the admin, from My devices
REASON_SIGN_OUT_OTHERS = "signed_out_others"    # My devices > Sign out all other devices
REASON_SUPER_ADMIN = "revoked_by_super_admin"   # Admins page > Devices
REASON_TRUSTED_LIMIT = "trusted_limit"          # a 6th device was trusted
REASON_PASSWORD_CHANGE = "password_change"      # TEOS-307 change password: every other device
REASON_PASSWORD_RESET = "password_reset"        # TEOS-307 reset link used: every device
REASON_ACCESS_REVOKED = "access_revoked"        # TEOS-307 revoke access: every device

DEVICES_TABLE = "admin_devices"
OPERATION_INSERT = "INSERT"
OPERATION_UPDATE = "UPDATE"
ACTION_VERIFIED = "device_verified"
ACTION_SIGNED_IN = "device_signed_in"
ACTION_RENAMED = "device_renamed"
ACTION_REVOKED = "device_revoked"
ACTION_BULK_REVOKED = "devices_revoked"
DEVICE_NOT_FOUND = "device_not_found"

TEMPLATE_NEW_DEVICE = "admin_new_device.htm"
TEMPLATE_DEVICE_REMOVED = "admin_device_removed.htm"
SUBJECT_NEW_DEVICE = "New sign-in to the admin portal"
SUBJECT_DEVICE_REMOVED = "A trusted device was removed from your admin account"

#: revoke_all reads at most this many active rows (an admin never has that many browsers)
REVOKE_ALL_LIMIT = 1000


# ---------- defaults (replaced in tests) ----------

def _default_audit():
    from app.services.admin.audit_service import AuditService
    return AuditService()


def smtp_configured() -> bool:
    """D7: the API mailer is used only when the SMTP account is set (config.py has placeholder defaults)."""
    return bool((os.getenv("SMTP_USERNAME") or "").strip() and (os.getenv("SMTP_PASSWORD") or "").strip())


def _default_mailer(subject: str, html_content: str, recipient: str) -> None:
    from app.config.config import send_email
    send_email(subject=subject, html_content=html_content, recipients=recipient)


#: ``app/email_templates`` of this checkout, used when ``EMAIL_TEMPLATES_PATH`` is not set (or empty)
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                              "email_templates")


def _default_renderer(template_name: str, data: Dict[str, Any]) -> Optional[str]:
    """Like ``config.get_email_template`` (same folder setting) but an empty setting falls back to the
    templates of this checkout. The values are HTML-escaped by the caller."""
    from jinja2 import Environment, FileSystemLoader
    folder = (os.getenv("EMAIL_TEMPLATES_PATH") or "").strip() or _TEMPLATES_DIR
    return Environment(loader=FileSystemLoader(folder)).get_template(template_name).render(data=data)


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True, name="admin-device-email").start()


# ---------- helpers ----------

def _parse_time(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        from app.config.utils import parse_iso_datetime
        parsed = parse_iso_datetime(str(value))
        if parsed is None:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utc_label(value: Any) -> str:
    moment = _parse_time(value)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if moment else ""


def _collapse_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    collapsed = " ".join(str(name).split())
    return collapsed or None


def _not_found():
    return coded(AdminNotFound, DEVICE_NOT_FOUND, ErrorMessages.ADMIN_DEVICE_NOT_FOUND)


def _with_version(name: Any, version: Any) -> str:
    return " ".join(part for part in (str(name or "").strip(), str(version or "").strip()) if part)


def _brand() -> Dict[str, str]:
    """The logo header of the emails, like the customer emails: ``{base_url}/logo/logo.png`` and the eSIM name, from
    the app settings (``BASE_URL``, ``ESIM_NAME``), else the environment."""
    def setting(key: str, default: str) -> str:
        try:
            from app.config.helper import get_config
            value = get_config(key, None)
        except Exception:
            value = None
        return str(value or os.getenv(key) or default)
    return {"base_url": html.escape(setting("BASE_URL", "").rstrip("/")),
            "esim_name": html.escape(setting("ESIM_NAME", "Monty eSIM"))}


class TrustedDeviceService:

    def __init__(self, repo_factory: Callable[[], AdminDeviceRepo] = AdminDeviceRepo,
                 audit_factory: Callable = _default_audit,
                 mailer: Callable[[str, str, str], None] = _default_mailer,
                 renderer: Callable[[str, Dict[str, Any]], Optional[str]] = _default_renderer,
                 background: Callable[[Callable[[], None]], None] = _in_thread,
                 smtp_check: Callable[[], bool] = smtp_configured,
                 clock: Callable[[], datetime] = utc_now):
        self._repo_factory = repo_factory
        self._audit_factory = audit_factory
        self._mailer = mailer
        self._renderer = renderer
        self._background = background
        self._smtp_check = smtp_check
        self._clock = clock
        self._repo_instance: Optional[AdminDeviceRepo] = None
        self._repo_lock = threading.Lock()

    def _repo(self) -> AdminDeviceRepo:
        """One repository (and Supabase client) per service instance, created on first use:
        ``session_device`` runs on every /admin request."""
        if self._repo_instance is None:
            with self._repo_lock:
                if self._repo_instance is None:
                    self._repo_instance = self._repo_factory()
        return self._repo_instance

    # ---------- called by the sign-in flow (BE-1) ----------

    def touch(self, device_id: str, ip: Optional[str]) -> None:
        """Refresh: ``last_used_at`` = now and ``last_ip``. No audit row; never raises (failures logged)."""
        try:
            self._repo().update(str(device_id), {"last_used_at": iso(self._clock()), "last_ip": ip})
        except Exception as exc:
            logger.error(f"admin devices: last use of {device_id} not saved: {type(exc).__name__}")

    def record_token(self, device_id: str, iat: Optional[int]) -> None:
        """Every access token the API hands out (trusted login, verify, refresh, change password): its ``iat``
        goes to ``last_token_iat``, keeping the max (conditional update, race-safe). The per-request check
        refuses a newer token of the same session: one refreshed directly at Supabase, outside the API.
        Raises on a store failure (the caller decides: a token whose iat is not stored would be refused)."""
        if iat is None:
            return
        self._repo().record_token_iat(str(device_id), int(iat))

    def find_trusted(self, admin_id: str, device_hash: str) -> Optional[Dict[str, Any]]:
        """The admin's active row for this hash when it is trusted now (``trusted_until`` in the future),
        else None (no row, untrusted row, or trust expired: a code is asked)."""
        row = self._repo().active_by_hash(admin_id, device_hash)
        return row if row and self._is_trusted(row) else None

    def signed_in_trusted(self, device: Dict[str, Any], session_id: str, ip: Optional[str],
                          info: AdminDeviceInfo) -> Dict[str, Any]:
        """Login on a trusted device (no code): the row gets the new ``session_id``, ``last_used_at`` = now,
        ``last_ip`` and the fresh display info. If the row held another live session, that session is ended
        first (one session per device). Returns the updated row. No email."""
        repo = self._repo()
        self._end_previous_session(repo, device, session_id)
        patch = {"session_id": session_id, "last_used_at": iso(self._clock()), "last_ip": ip,
                 "device_info": display_info(info)}
        row = repo.update(str(device["id"]), patch) or {**device, **patch}
        admin_id = str(device.get("admin_id") or "")
        self._audit(AuditContext(actor_id=admin_id, ip=ip), OPERATION_UPDATE, row, ACTION_SIGNED_IN,
                    admin_id=admin_id, trusted_until=row.get("trusted_until"))
        return row

    def register_verified(self, admin_id: str, email: Optional[str], device_hash: str, info: AdminDeviceInfo,
                          session_id: str, ip: Optional[str], trust: bool, name: Optional[str],
                          ctx: AuditContext) -> Dict[str, Any]:
        """After a correct email code: create the row (or update the admin's active row for this hash),
        set ``session_id``, ``last_used_at``, ``last_ip`` (``first_ip`` on create), display info, ``name`` when
        given; ``trusted_until`` = now + TRUST_DAYS when ``trust`` else null. Enforces MAX_TRUSTED (revokes the
        least recently used trusted one + email naming it). Sends the "new device signed in" email (browser,
        OS, IP, time) and writes the audit row. Returns the row."""
        repo = self._repo()
        now = self._clock()
        name = _collapse_name(name)
        patch: Dict[str, Any] = {
            "session_id": session_id, "last_used_at": iso(now), "last_ip": ip, "device_info": display_info(info),
            "trusted_until": iso(now + timedelta(days=TRUST_DAYS)) if trust else None,
        }
        if name:
            patch["name"] = name
        existing = repo.active_by_hash(admin_id, device_hash)
        if existing:
            self._end_previous_session(repo, existing, session_id)
            row = repo.update(str(existing["id"]), patch) or {**existing, **patch}
            operation = OPERATION_UPDATE
        else:
            row = repo.insert({"admin_id": admin_id, "device_hash": device_hash, "first_ip": ip, **patch})
            operation = OPERATION_INSERT
        self._audit(ctx, operation, row, ACTION_VERIFIED, admin_id=admin_id, trusted=bool(trust),
                    trusted_until=row.get("trusted_until"), new_row=existing is None)
        if trust:
            self._enforce_trusted_limit(repo, admin_id, email, row, ctx)
        self._send(TEMPLATE_NEW_DEVICE, SUBJECT_NEW_DEVICE, email, self._new_device_data(row, now))
        return row

    def session_device(self, session_id: str) -> Optional[Dict[str, Any]]:
        """The row of this session for the per-request check: active row, else the latest revoked one
        (so the caller can answer ``device_revoked``), else None."""
        if not session_id:
            return None
        return self._repo().by_session(str(session_id))

    def move_session(self, device_id: str, new_session_id: str) -> None:
        """TEOS-307 change password: the caller's row now holds the new session (the old one was signed out)."""
        self._repo().update(str(device_id), {"session_id": new_session_id,
                                                      "last_used_at": iso(self._clock())})

    def clear_session(self, session_id: str) -> None:
        """Logout: the row keeps its trust but no longer has a session."""
        if not session_id:
            return
        repo = self._repo()
        row = repo.by_session(str(session_id))
        if row and not row.get("revoked_at"):
            repo.update(str(row["id"]), {"session_id": None})

    def revoke_all(self, admin_id: str, reason: str, ctx: AuditContext,
                   except_device_id: Optional[str] = None) -> int:
        """Revokes every active row of the admin (except one), ending each session. Returns how many.
        Used by: sign out all others, password change (except the caller's), password reset and revoke
        access (all)."""
        repo = self._repo()
        keep = str(except_device_id).lower() if except_device_id else None
        ids = [str(row["id"]) for row in repo.list_active(admin_id, limit=REVOKE_ALL_LIMIT)
               if str(row["id"]).lower() != keep]
        revoked = self._revoke_rows(repo, ids, ctx.actor_id, reason)
        if revoked:
            self._audit(ctx, OPERATION_UPDATE, None, ACTION_BULK_REVOKED, admin_id=admin_id, reason=reason,
                        by=ctx.actor_id, count=len(revoked), device_ids=[str(row["id"]) for row in revoked],
                        except_device_id=except_device_id)
        return len(revoked)

    # ---------- the devices pages (BE-2 endpoints) ----------

    def list_devices(self, admin_id: str, current_session_id: Optional[str] = None) -> List[AdminDeviceRow]:
        rows = self._repo().list_active(admin_id)
        return [self._to_row(row, current_session_id) for row in rows]

    def rename(self, admin_id: str, device_id: str, name: str, ctx: AuditContext, *,
               current_session_id: Optional[str] = None) -> AdminDeviceRow:
        """404 ``device_not_found`` when the row is not an active row of ``admin_id``. ``name`` 1..60 after
        collapsing spaces (the request schema checks it). ``current_session_id`` only sets ``current``."""
        repo = self._repo()
        row = repo.get_active(admin_id, str(device_id))
        if not row:
            raise _not_found()
        new_name = _collapse_name(name)
        updated = repo.update(str(row["id"]), {"name": new_name}) or {**row, "name": new_name}
        self._audit(ctx, OPERATION_UPDATE, updated, ACTION_RENAMED, admin_id=admin_id,
                    old={"name": row.get("name")}, new={"name": new_name})
        return self._to_row(updated, current_session_id)

    def revoke(self, admin_id: str, device_id: str, reason: str, ctx: AuditContext) -> None:
        """404 ``device_not_found`` when the row is not an active row of ``admin_id``."""
        repo = self._repo()
        row = repo.get_active(admin_id, str(device_id))
        if not row:
            raise _not_found()
        revoked = self._revoke_rows(repo, [str(row["id"])], ctx.actor_id, reason)
        if not revoked:  # revoked by someone else between the read and the write
            raise _not_found()
        self._audit(ctx, OPERATION_UPDATE, revoked[0], ACTION_REVOKED, admin_id=admin_id, reason=reason,
                    by=ctx.actor_id)

    # ---------- internals ----------

    def _is_trusted(self, row: Dict[str, Any]) -> bool:
        until = _parse_time(row.get("trusted_until"))
        return until is not None and until > self._clock()

    def _to_row(self, row: Dict[str, Any], current_session_id: Optional[str]) -> AdminDeviceRow:
        info = row.get("device_info") or {}
        session_id = row.get("session_id")
        current = bool(session_id and current_session_id
                       and str(session_id).lower() == str(current_session_id).lower())
        return AdminDeviceRow(
            id=str(row["id"]), name=row.get("name"),
            browser=info.get("browser") or "", browser_version=info.get("browser_version") or "",
            os=info.get("os") or "", os_version=info.get("os_version") or "",
            timezone=info.get("timezone") or "", screen=info.get("screen") or "",
            language=info.get("language") or "",
            first_ip=row.get("first_ip"), last_ip=row.get("last_ip"),
            created_at=row.get("created_at") or row.get("last_used_at"),
            last_used_at=row.get("last_used_at") or row.get("created_at"),
            trusted=self._is_trusted(row), trusted_until=row.get("trusted_until"),
            signed_in=session_id is not None, current=current)

    def _end_previous_session(self, repo: AdminDeviceRepo, row: Dict[str, Any], new_session_id: str) -> None:
        old = row.get("session_id")
        if old and str(old).lower() != str(new_session_id).lower():
            self._end_session(repo, str(old))

    @staticmethod
    def _end_session(repo: AdminDeviceRepo, session_id: str) -> None:
        try:
            repo.end_session(session_id)
        except Exception as exc:  # the row is revoked / moved anyway: the per-request check refuses it
            logger.error(f"admin devices: could not end session: {type(exc).__name__}")

    def _revoke_rows(self, repo: AdminDeviceRepo, ids: Iterable[str], revoked_by: Optional[str],
                     reason: str) -> List[Dict[str, Any]]:
        ids = list(ids)
        if not ids:
            return []
        revoked = repo.mark_revoked(ids, revoked_by, reason)
        for row in revoked:
            if row.get("session_id"):
                self._end_session(repo, str(row["session_id"]))
        return revoked

    def _enforce_trusted_limit(self, repo: AdminDeviceRepo, admin_id: str, email: Optional[str],
                               new_row: Dict[str, Any], ctx: AuditContext) -> None:
        """At most MAX_TRUSTED trusted rows: the least recently used other ones are revoked."""
        try:
            new_id = str(new_row.get("id"))
            others = [row for row in repo.trusted_active(admin_id) if str(row["id"]) != new_id]
            excess = others[:max(0, len(others) - (MAX_TRUSTED - 1))]   # trusted_active: oldest first
            for victim in excess:
                revoked = self._revoke_rows(repo, [str(victim["id"])], ctx.actor_id, REASON_TRUSTED_LIMIT)
                if not revoked:
                    continue
                self._audit(ctx, OPERATION_UPDATE, revoked[0], ACTION_REVOKED, admin_id=admin_id,
                            reason=REASON_TRUSTED_LIMIT, by=ctx.actor_id)
                self._send(TEMPLATE_DEVICE_REMOVED, SUBJECT_DEVICE_REMOVED, email,
                           self._removed_device_data(victim, new_row))
        except Exception as exc:
            logger.error(f"admin devices: trusted limit not enforced for {admin_id}: {type(exc).__name__}: {exc}")

    def _audit(self, ctx: AuditContext, operation: str, row: Optional[Dict[str, Any]], action: str,
               **details: Any) -> None:
        if row is not None:
            info = row.get("device_info") or {}
            details.setdefault("device_id", str(row.get("id")))
            details.setdefault("device_name", row.get("name"))
            details.setdefault("device", device_label(info))
            details.setdefault("device_ip", row.get("last_ip"))
        old = details.pop("old", None)
        new = details.pop("new", None)
        try:
            self._audit_factory().log(ctx, DEVICES_TABLE, operation,
                                      record_id=str(row["id"]) if row is not None else None,
                                      old=old, new=new, action=action, **details)
        except Exception as exc:
            logger.error(f"admin devices: audit '{action}' not written: {type(exc).__name__}: {exc}")

    # ---------- emails ----------

    @staticmethod
    def _new_device_data(row: Dict[str, Any], when: datetime) -> Dict[str, Any]:
        info = row.get("device_info") or {}
        return {"device": row.get("name") or device_label(info),
                "browser": _with_version(info.get("browser"), info.get("browser_version")) or "Unknown",
                "os": _with_version(info.get("os"), info.get("os_version")) or "Unknown",
                "ip": row.get("last_ip") or "Unknown",
                "time": _utc_label(when),
                "trusted": bool(row.get("trusted_until"))}

    @staticmethod
    def _removed_device_data(victim: Dict[str, Any], new_row: Dict[str, Any]) -> Dict[str, Any]:
        info = victim.get("device_info") or {}
        new_info = new_row.get("device_info") or {}
        return {"device": victim.get("name") or device_label(info),
                "browser": _with_version(info.get("browser"), info.get("browser_version")) or "Unknown",
                "os": _with_version(info.get("os"), info.get("os_version")) or "Unknown",
                "ip": victim.get("last_ip") or "Unknown",
                "time": _utc_label(victim.get("last_used_at")),
                "max_trusted": MAX_TRUSTED,
                "new_device": new_row.get("name") or device_label(new_info)}

    def _send(self, template: str, subject: str, recipient: Optional[str], data: Dict[str, Any]) -> None:
        """Renders and sends in the background; never raises. Skipped when SMTP is not set or no address."""
        try:
            if not recipient:
                logger.info(f"admin devices: email '{template}' skipped: the admin has no email address")
                return
            if not self._smtp_check():
                logger.info(f"admin devices: email '{template}' skipped: SMTP is not configured")
                return
            safe = {key: html.escape(value) if isinstance(value, str) else value for key, value in data.items()}

            def job() -> None:
                try:
                    content = self._renderer(template, {**_brand(), **safe})
                    if not content:
                        logger.error(f"admin devices: email template '{template}' could not be rendered")
                        return
                    self._mailer(subject, content, recipient)
                except Exception as exc:
                    logger.error(f"admin devices: email '{template}' not sent: {type(exc).__name__}")

            self._background(job)
        except Exception as exc:
            logger.error(f"admin devices: email '{template}' not queued: {type(exc).__name__}")
