"""Users page and user detail of the admin portal (MS-5079).

- list and detail: one RPC (``admin_search_users``) joins users_copy with auth.users, so the ban
  state comes with the rows instead of one GoTrue call per row (MS-5067), with stable paging and
  the tab counts in the same call;
- ban and unban: done server-side in Supabase Auth (``update_user_by_id`` with ``ban_duration``),
  then audited through ``AuditService.log`` with the actor from the token and the IP from the
  request (MS-5071). A ban and its audit row are not one transaction: when the audit insert
  fails, the previous ban state is restored and 502 returned;
- ban also signs the user out everywhere (TEOS-111 case 9, see ``revoke_user_sessions``). A revoke
  failure keeps the ban (the safe state: GoTrue refuses sign-in and refresh of a banned user),
  writes the audit row with ``sessions_revoked: false`` and the cause, and returns 502
  ``session_revoke_failed``. Banning that user again then re-runs only the revoke (TEOS-149):
  200 with the current ban when it works, audited as a BAN row with ``revoke_retry: true``;
- an admin cannot ban or unban themselves: 409 ``cannot_ban_self``, checked first, so nothing is
  read, changed or audited (TEOS-111 case 23);
- devices, wallet transactions and referrals: paged sub-lists with the true total; 404
  ``user_not_found`` for an unknown user, 200 empty for a user without rows (TEOS-153).
"""
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from gotrue.errors import AuthApiError
from loguru import logger

from app.config.config import supabase_client
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError
from app.repo.admin.base import AuditContext
from app.repo.admin.user_repo import UserAdminRepo, UserWalletAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.users import AdminUserBan, AdminUserUnban
from app.services.admin.audit_service import AuditService
from app.services.admin.device_service import DeviceAdminService

#: audit_log.table_name of ban and unban rows (the legacy portal used the same value).
AUTH_USERS_TABLE = "auth.users"
OPERATION_BAN = "BAN"
OPERATION_UNBAN = "UNBAN"
#: What GoTrue expects to lift a ban, instead of a "<hours>h" duration.
UNBAN_DURATION = "none"
AUTH_SERVICE_UNAVAILABLE = "AUTH_SERVICE_UNAVAILABLE"
AUDIT_FAILED = "AUDIT_FAILED"
#: 502 code when the ban was applied but the user's sessions could not be revoked.
SESSION_REVOKE_FAILED = "session_revoke_failed"
#: 409 code when an admin targets their own account.
CANNOT_BAN_SELF = "cannot_ban_self"


class SessionRevokeError(Exception):
    """The user's sessions could not be revoked; ``reason`` is a short machine-readable cause."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def revoke_user_sessions(repo: UserAdminRepo, user_id: str) -> Dict[str, int]:
    """Signs ``user_id`` out of every session: all sessions and refresh tokens deleted (TEOS-149).

    GoTrue (v2.197) has no admin "log out user X" route, so ``admin_revoke_user_sessions`` deletes
    the rows GoTrue's own global logout deletes. Afterwards every refresh token of the user is
    refused and GoTrue answers ``/user`` with ``session_not_found`` for every access token issued
    before. No JWT is involved, so it works with symmetric and asymmetric signing keys alike.
    Returns ``{sessions, refresh_tokens}`` (0 and 0 for a user without sessions); raises
    ``SessionRevokeError`` when the database call fails.
    """
    try:
        return repo.revoke_sessions(str(user_id))
    except Exception as exc:
        raise SessionRevokeError(f"database_error:{type(exc).__name__}") from exc


def revoke_pending(ban_audit: Any) -> bool:
    """True when ``ban_audit`` (the user's newest BAN audit row) records a revoke that failed, i.e.
    ``operation_details.sessions_revoked`` is exactly ``false``. Legacy rows without the flag: False."""
    if not isinstance(ban_audit, dict):
        return False
    details = ban_audit.get("operation_details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            return False
    return isinstance(details, dict) and details.get("sessions_revoked") is False


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse(value: Any) -> Optional[datetime]:
    """A timezone-aware datetime from GoTrue (datetime) or the RPC (ISO text); None when unreadable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def remaining_ban_duration(previous_banned_until: Any, now: Optional[datetime] = None) -> Optional[str]:
    """GoTrue duration (``"<seconds>s"``) that re-applies a ban until ``previous_banned_until``;
    None when that date is unreadable or already in the past."""
    until = _parse(previous_banned_until)
    if until is None:
        return None
    seconds = math.ceil((until - (now or datetime.now(timezone.utc))).total_seconds())
    return f"{seconds}s" if seconds > 0 else None


class UserAdminService:
    def __init__(self, repo: Optional[UserAdminRepo] = None, wallets: Optional[UserWalletAdminRepo] = None,
                 devices: Optional[DeviceAdminService] = None, audit: Optional[AuditService] = None,
                 client_factory: Callable = supabase_client,
                 session_revoker: Optional[Callable[[str], Any]] = None):
        self._repo = repo
        self._wallets = wallets
        self._devices = devices
        self._audit = audit
        self._client_factory = client_factory
        self._session_revoker = session_revoker or (
            lambda user_id: revoke_user_sessions(self.repo, user_id))

    # created lazily so importing the router never opens a database client
    @property
    def repo(self) -> UserAdminRepo:
        if self._repo is None:
            self._repo = UserAdminRepo()
        return self._repo

    @property
    def wallets(self) -> UserWalletAdminRepo:
        if self._wallets is None:
            self._wallets = UserWalletAdminRepo()
        return self._wallets

    @property
    def devices(self) -> DeviceAdminService:
        if self._devices is None:
            self._devices = DeviceAdminService()
        return self._devices

    @property
    def audit(self) -> AuditService:
        if self._audit is None:
            self._audit = AuditService()
        return self._audit

    # ---------- reads ----------

    def list(self, search: Optional[str], ban_status: str, page: PageParams, *,
             sort: str = "created_at", direction: str = "desc") -> AdminPage:
        result = self.repo.search(search, ban_status, page, sort=sort, direction=direction)
        return AdminPage(data=result.rows, total_count=result.total, facets=result.facets)

    def get(self, user_id: str) -> Dict[str, Any]:
        """The user row plus ``wallet: {amount, currency}`` (None without a wallet). 404 when unknown."""
        row = self._row_or_404(user_id)
        return {**row, "wallet": self.wallets.for_user(user_id)}

    def list_devices(self, user_id: str, page: PageParams) -> AdminPage:
        self._row_or_404(user_id)
        return self.devices.list(page, user_id=str(user_id))

    def list_wallet_transactions(self, user_id: str, page: PageParams) -> AdminPage:
        self._row_or_404(user_id)
        result = self.repo.wallet_transactions(user_id, page)
        return AdminPage(data=result.rows, total_count=result.total)

    def list_referrals(self, user_id: str, page: PageParams, *, search: Optional[str] = None,
                       from_email: Optional[str] = None, referral_date: Optional[date] = None) -> AdminPage:
        self._row_or_404(user_id)
        result = self.repo.referrals(user_id, page, search=search, from_email=from_email,
                                     referral_date=referral_date)
        return AdminPage(data=result.rows, total_count=result.total)

    # ---------- ban / unban ----------

    def ban(self, user_id: str, body: AdminUserBan, ctx: AuditContext) -> Dict[str, Any]:
        """409 ``cannot_ban_self``; 404 unknown user; 409 ``already_banned``; 502 when GoTrue fails
        (nothing audited) or when the audit insert fails (the ban is lifted again); 502
        ``session_revoke_failed`` when the ban is applied and audited but the sessions could not be
        revoked (the ban stays). A user already banned whose newest BAN audit row has
        ``sessions_revoked: false``: only the revoke is re-run (see ``_retry_revoke``)."""
        self._refuse_self(user_id, ctx)
        row = self._row_or_404(user_id)
        if row.get("is_banned"):
            last_ban = self.repo.latest_ban_audit(str(user_id))
            if revoke_pending(last_ban):
                return self._retry_revoke(user_id, row, last_ban, body, ctx)
            raise AdminConflict("already_banned", banned_until=row.get("banned_until"))
        # TEOS-150: the raw earlier value, also when that ban already ended (None when never banned)
        previous_banned_until = _iso(row.get("banned_until"))
        duration = f"{body.hours}h"
        user = self._set_ban_duration(user_id, duration)
        banned_until = self._banned_until_after_ban(user_id, user, body.hours)
        email = getattr(user, "email", None) or row.get("email")
        revoke_error = self._revoke_sessions(user_id)
        try:
            self.audit.log(ctx, AUTH_USERS_TABLE, OPERATION_BAN, record_id=user_id, rows_affected=1,
                           user_id=str(user_id), user_email=email, hours_banned=body.hours,
                           ban_duration=duration, banned_until=banned_until,
                           previous_banned_until=previous_banned_until,
                           banned_at=datetime.now(timezone.utc).isoformat(),
                           sessions_revoked=revoke_error is None, session_revoke_error=revoke_error)
        except Exception as exc:
            logger.error(f"admin ban: audit insert failed, lifting the ban again: {type(exc).__name__}: {exc}")
            self._compensate(user_id, UNBAN_DURATION)
            raise AdminUpstreamError(AUDIT_FAILED) from exc
        if revoke_error is not None:
            raise AdminUpstreamError(SESSION_REVOKE_FAILED)
        return {"user_id": str(user_id), "email": email, "banned_until": banned_until, "hours_banned": body.hours}

    def unban(self, user_id: str, body: AdminUserUnban, ctx: AuditContext) -> Dict[str, Any]:
        """409 ``cannot_ban_self``; 404 unknown user; 409 ``not_banned`` (a lapsed ban is not a ban);
        502 when GoTrue fails (nothing audited) or when the audit insert fails (the previous ban is
        re-applied)."""
        self._refuse_self(user_id, ctx)
        row = self._row_or_404(user_id)
        if not row.get("is_banned"):
            raise AdminConflict("not_banned")
        previous_banned_until = _iso(row.get("banned_until"))
        user = self._set_ban_duration(user_id, UNBAN_DURATION)
        email = getattr(user, "email", None) or row.get("email")
        try:
            self.audit.log(ctx, AUTH_USERS_TABLE, OPERATION_UNBAN, record_id=user_id, rows_affected=1,
                           user_id=str(user_id), user_email=email, hours_banned=None,
                           ban_duration=UNBAN_DURATION, previous_banned_until=previous_banned_until,
                           unbanned_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            logger.error(f"admin unban: audit insert failed, re-applying the ban: {type(exc).__name__}: {exc}")
            restore = remaining_ban_duration(previous_banned_until)
            if restore:
                self._compensate(user_id, restore)
            raise AdminUpstreamError(AUDIT_FAILED) from exc
        return {"user_id": str(user_id), "email": email}

    def _retry_revoke(self, user_id: str, row: Dict[str, Any], last_ban: Dict[str, Any], body: AdminUserBan,
                      ctx: AuditContext) -> Dict[str, Any]:
        """Re-runs only the session revoke of a ban whose revoke failed (TEOS-149). The ban itself is
        not changed: ``body.hours`` is ignored and the current ban is returned. Audited as a BAN row
        with ``revoke_retry: true`` and ``rows_affected: 0``, so a later ban answers 409
        ``already_banned`` once the revoke worked. 502 ``session_revoke_failed`` when it fails again
        (audited, retry possible), 502 ``AUDIT_FAILED`` when the audit row cannot be written."""
        details = last_ban.get("operation_details")
        if isinstance(details, str):
            details = json.loads(details)
        hours = details.get("hours_banned")
        hours = hours if isinstance(hours, int) and not isinstance(hours, bool) else body.hours
        banned_until = _iso(row.get("banned_until"))
        email = row.get("email")
        revoke_error = self._revoke_sessions(user_id)
        try:
            self.audit.log(ctx, AUTH_USERS_TABLE, OPERATION_BAN, record_id=user_id, rows_affected=0,
                           user_id=str(user_id), user_email=email, hours_banned=hours,
                           ban_duration=details.get("ban_duration"), banned_until=banned_until,
                           previous_banned_until=banned_until,  # TEOS-150: raw value, as on unban
                           revoke_retry=True, retry_of_audit_id=last_ban.get("id"),
                           sessions_revoked=revoke_error is None, session_revoke_error=revoke_error)
        except Exception as exc:
            logger.error(f"admin ban: revoke retry audit insert failed: {type(exc).__name__}: {exc}")
            raise AdminUpstreamError(AUDIT_FAILED) from exc
        if revoke_error is not None:
            raise AdminUpstreamError(SESSION_REVOKE_FAILED)
        return {"user_id": str(user_id), "email": email, "banned_until": banned_until, "hours_banned": hours}

    # ---------- helpers ----------

    @staticmethod
    def _refuse_self(user_id: str, ctx: AuditContext) -> None:
        """An admin banning (or unbanning) their own account would lock themselves out (or lift a
        ban someone else set): refused before anything is read."""
        if ctx.actor_id and str(user_id).lower() == str(ctx.actor_id).lower():
            raise AdminConflict(CANNOT_BAN_SELF)

    def _revoke_sessions(self, user_id: str) -> Optional[str]:
        """None when every session of the user was revoked, else the cause (logged, then audited)."""
        try:
            deleted = self._session_revoker(str(user_id))
            logger.info(f"admin ban: sessions of {user_id} revoked: {deleted}")
        except SessionRevokeError as exc:
            logger.error(f"admin ban: could not revoke the sessions of {user_id}: {exc.reason}")
            return exc.reason
        except Exception as exc:
            logger.error(f"admin ban: could not revoke the sessions of {user_id}: {type(exc).__name__}: {exc}")
            return f"unexpected:{type(exc).__name__}"
        return None

    def _banned_until_after_ban(self, user_id: str, user: Any, hours: int) -> str:
        """The end of the ban just applied. gotrue-py's User model drops GoTrue's ``banned_until``, so
        it is read back from the users row (``admin_search_users`` joins auth.users); when that read
        fails or has no date, now + ``hours``. The ban is already applied, so this never raises."""
        value = getattr(user, "banned_until", None)
        if value is None:
            try:
                value = (self.repo.get(str(user_id)) or {}).get("banned_until")
            except Exception as exc:
                logger.error(f"admin ban: could not read banned_until back for {user_id}: {type(exc).__name__}")
        if _parse(value) is None:
            value = datetime.now(timezone.utc) + timedelta(hours=hours)
        return _iso(value)

    def _row_or_404(self, user_id: str) -> Dict[str, Any]:
        row = self.repo.get(str(user_id))
        if row is None:
            raise AdminNotFound("user_not_found")
        return row

    def _set_ban_duration(self, user_id: str, duration: str) -> Any:
        """``update_user_by_id(ban_duration)`` in Supabase Auth; returns the updated GoTrue user."""
        try:
            response = self._client_factory().auth.admin.update_user_by_id(str(user_id), {"ban_duration": duration})
        except AuthApiError as exc:
            if (getattr(exc, "status", 0) or 0) == 404:
                raise AdminNotFound("user_not_found") from exc
            logger.error(f"admin ban: Supabase Auth failed: {type(exc).__name__}: {exc}")
            raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE) from exc
        except Exception as exc:  # network, timeout, unexpected client error
            logger.error(f"admin ban: Supabase Auth failed: {type(exc).__name__}: {exc}")
            raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE) from exc
        return getattr(response, "user", None)

    def _compensate(self, user_id: str, duration: str) -> None:
        """Best effort: put the ban state back after a failed audit insert. A failure here is logged
        only; the caller still returns 502."""
        try:
            self._client_factory().auth.admin.update_user_by_id(str(user_id), {"ban_duration": duration})
        except Exception as exc:
            logger.error(f"admin ban: could not restore the ban state of {user_id} ({duration}): "
                         f"{type(exc).__name__}: {exc}")
