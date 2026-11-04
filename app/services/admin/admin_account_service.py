"""Admins page (TEOS-307, super admin only through ``admin.security.manage``).

- list: the accounts whose ``app_metadata.role`` is an admin role. The role lives only in Supabase Auth
  (``users_copy`` has no role and the API's database user cannot read ``auth.users``), so the accounts are
  read through the GoTrue admin API page by page and filtered here. Search, role tab and paging are done in
  memory over the admins found. No database change.
- reset password: Supabase Auth emails the admin a single-use reset link to the portal page set in
  ``ADMIN_PASSWORD_RESET_URL``. Nothing changes on the account until the admin sets the new password there
  (``POST /auth/reset-password``), which then signs out every session of the account.
- revoke access: the admin role is removed (the account is kept: it may also be an app customer) and every
  session of the account is signed out. The audit row is written after the change; when it cannot be
  written the role is put back and 502 is returned, like a ban.

A super admin cannot reset or revoke their own account (409 ``cannot_target_self``): their own password is
changed from the Profile page. That also keeps at least one super admin.
"""
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from gotrue.errors import AuthApiError, AuthError
from loguru import logger

from app.config.admin_permissions import ROLE_SUPER_ADMIN, permissions_for
from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminRateLimited, AdminUpstreamError
from app.repo.admin.base import AuditContext
from app.schemas.admin.admins import (AdminAccessRevoked, AdminAccountRow, AdminPasswordResetSent,
                                      AdminRoleFilter)
from app.schemas.admin.common import AdminPage, PageParams
from app.services.admin.auth_service import (AUTH_SERVICE_UNAVAILABLE, AUTH_USERS_TABLE, OPERATION_UPDATE,
                                             _default_audit, _default_session_revoker, _is_upstream_failure)

#: GoTrue admin list page size, and a safety cap on pages read per request (100k accounts)
LIST_PAGE_SIZE = 1000
LIST_MAX_PAGES = 100

CANNOT_TARGET_SELF = "cannot_target_self"
ADMIN_NOT_FOUND = "admin_not_found"
ADMIN_HAS_NO_EMAIL = "admin_has_no_email"
RESET_URL_NOT_SET = "PASSWORD_RESET_URL_NOT_SET"
RESET_EMAIL_FAILED = "RESET_EMAIL_FAILED"
RESET_EMAIL_RATE_LIMITED = "reset_email_rate_limited"
AUDIT_FAILED = "AUDIT_FAILED"
ACTION_RESET_EMAIL_SENT = "password_reset_email_sent"
ACTION_ACCESS_REVOKED = "admin_access_revoked"


def _role_of(user: Any) -> Optional[str]:
    role = (getattr(user, "app_metadata", None) or {}).get("role")
    return role if permissions_for(role) else None


def _reset_url() -> Optional[str]:
    return (os.getenv("ADMIN_PASSWORD_RESET_URL") or "").strip() or None


def _is_rate_limited(exc: AuthError) -> bool:
    return (getattr(exc, "status", None) == 429
            or getattr(exc, "code", None) in ("over_email_send_rate_limit", "over_request_rate_limit"))


class AdminAccountService:
    def __init__(self, client_factory: Callable = supabase_client,
                 session_revoker: Callable[[str], Dict[str, int]] = _default_session_revoker,
                 audit_factory: Callable = _default_audit,
                 reset_url: Callable[[], Optional[str]] = _reset_url):
        self._client_factory = client_factory
        self._session_revoker = session_revoker
        self._audit_factory = audit_factory
        self._reset_url = reset_url

    # ---------- list ----------

    def list(self, search: Optional[str], role: AdminRoleFilter, page: PageParams,
             current_user_id: str) -> AdminPage:
        """Super admins first, then by email (case-insensitive), then id. ``facets`` = {all, super_admin,
        admin} over the searched set, so the tab counts follow the search."""
        rows = [self._row(user, current_user_id) for user in self._all_admins()]
        needle = (search or "").strip().lower()
        if needle:
            rows = [row for row in rows if needle in (row.email or "").lower()]
        facets = {"all": len(rows),
                  "super_admin": sum(1 for row in rows if row.role == ROLE_SUPER_ADMIN),
                  "admin": sum(1 for row in rows if row.role != ROLE_SUPER_ADMIN)}
        if role != "all":
            rows = [row for row in rows if row.role == role]
        rows.sort(key=lambda row: (row.role != ROLE_SUPER_ADMIN, (row.email or "").lower(), row.id))
        return AdminPage(data=rows[page.offset:page.offset + page.limit], total_count=len(rows), facets=facets)

    def _all_admins(self) -> List[Any]:
        client = self._client_factory()
        admins: List[Any] = []
        for number in range(1, LIST_MAX_PAGES + 1):
            try:
                users = client.auth.admin.list_users(page=number, per_page=LIST_PAGE_SIZE) or []
            except AuthError as exc:
                logger.error(f"admins list: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            admins.extend(user for user in users if _role_of(user))
            if len(users) < LIST_PAGE_SIZE:
                break
        else:
            logger.warning(f"admins list: stopped after {LIST_MAX_PAGES} pages of {LIST_PAGE_SIZE} accounts")
        return admins

    @staticmethod
    def _row(user: Any, current_user_id: str) -> AdminAccountRow:
        user_id = str(user.id)
        return AdminAccountRow(id=user_id, email=getattr(user, "email", None), role=_role_of(user),
                               created_at=getattr(user, "created_at", None),
                               last_sign_in_at=getattr(user, "last_sign_in_at", None),
                               is_current_user=user_id.lower() == str(current_user_id).lower())

    # ---------- reset password ----------

    def send_password_reset(self, admin_id: str, ctx: AuditContext) -> AdminPasswordResetSent:
        """409 ``cannot_target_self``; 404 ``admin_not_found`` (unknown account or no admin role);
        409 ``admin_has_no_email``; 502 ``PASSWORD_RESET_URL_NOT_SET``; 429 when Supabase's email rate
        limit is reached; 502 ``RESET_EMAIL_FAILED`` when the email cannot be sent (e.g. no SMTP)."""
        user = self._target(admin_id, ctx)
        redirect_to = self._reset_url()
        if not redirect_to:
            logger.error("admin password reset: ADMIN_PASSWORD_RESET_URL is not set")
            raise AdminUpstreamError(RESET_URL_NOT_SET, name=ErrorMessages.ADMIN_RESET_NOT_CONFIGURED)
        email = getattr(user, "email", None)
        if not email:
            raise AdminConflict(ADMIN_HAS_NO_EMAIL)
        try:
            self._client_factory().auth.reset_password_for_email(email, {"redirect_to": redirect_to})
        except AuthError as exc:
            if _is_rate_limited(exc):
                raise AdminRateLimited(RESET_EMAIL_RATE_LIMITED, name=ErrorMessages.ADMIN_RESET_EMAIL_RATE_LIMITED)
            logger.error(f"admin password reset: email not sent: {type(exc).__name__} "
                         f"{getattr(exc, 'code', None)}")
            raise AdminUpstreamError(RESET_EMAIL_FAILED, name=ErrorMessages.ADMIN_RESET_EMAIL_FAILED)
        try:
            self._audit_factory().log(ctx, AUTH_USERS_TABLE, OPERATION_UPDATE, record_id=str(user.id),
                                      rows_affected=0, action=ACTION_RESET_EMAIL_SENT, user_id=str(user.id),
                                      user_email=email, sent_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:  # the email is already on its way
            logger.error(f"admin password reset: audit insert failed: {type(exc).__name__}: {exc}")
        return AdminPasswordResetSent(id=str(user.id), email=email)

    # ---------- revoke access ----------

    def revoke_access(self, admin_id: str, ctx: AuditContext) -> AdminAccessRevoked:
        """409 ``cannot_target_self``; 404 ``admin_not_found``; 502 when Supabase Auth fails (nothing
        changed) or when the audit row cannot be written (the role is put back). ``sessions_signed_out``
        false: the role is removed, so the API refuses the account's next request anyway (403), but its
        sessions could not be deleted."""
        user = self._target(admin_id, ctx)
        user_id = str(user.id)
        previous_role = _role_of(user)
        client = self._client_factory()
        self._set_role(client, user_id, None)
        signed_out = True
        try:
            self._session_revoker(user_id)
        except Exception as exc:
            logger.error(f"admin revoke access: could not revoke the sessions of {user_id}: {type(exc).__name__}")
            signed_out = False
        try:
            self._audit_factory().log(ctx, AUTH_USERS_TABLE, OPERATION_UPDATE, record_id=user_id, rows_affected=1,
                                      old={"role": previous_role}, new={"role": None},
                                      action=ACTION_ACCESS_REVOKED, user_id=user_id,
                                      user_email=getattr(user, "email", None), sessions_signed_out=signed_out,
                                      revoked_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            logger.error(f"admin revoke access: audit insert failed, putting the role back: {type(exc).__name__}")
            try:
                self._set_role(client, user_id, previous_role)
            except Exception:
                logger.error(f"admin revoke access: could not put the role of {user_id} back")
            raise AdminUpstreamError(AUDIT_FAILED) from exc
        return AdminAccessRevoked(id=user_id, email=getattr(user, "email", None), previous_role=previous_role,
                                  sessions_signed_out=signed_out)

    # ---------- helpers ----------

    def _target(self, admin_id: str, ctx: AuditContext) -> Any:
        """The admin account ``admin_id``, never the caller's own."""
        if ctx.actor_id and str(admin_id).lower() == str(ctx.actor_id).lower():
            raise AdminConflict(CANNOT_TARGET_SELF, name=ErrorMessages.ADMIN_CANNOT_TARGET_SELF)
        try:
            response = self._client_factory().auth.admin.get_user_by_id(str(admin_id))
        except AuthError as exc:
            if isinstance(exc, AuthApiError) and getattr(exc, "status", None) == 404:
                raise AdminNotFound(ADMIN_NOT_FOUND, name=ErrorMessages.ADMIN_ACCOUNT_NOT_FOUND)
            if _is_upstream_failure(exc):
                logger.error(f"admins: Supabase Auth failed: {type(exc).__name__}")
                raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
            raise AdminNotFound(ADMIN_NOT_FOUND, name=ErrorMessages.ADMIN_ACCOUNT_NOT_FOUND)
        user = getattr(response, "user", None)
        if user is None or not _role_of(user):
            raise AdminNotFound(ADMIN_NOT_FOUND, name=ErrorMessages.ADMIN_ACCOUNT_NOT_FOUND)
        return user

    @staticmethod
    def _set_role(client, user_id: str, role: Optional[str]) -> None:
        """GoTrue merges ``app_metadata``: a null value removes the key, the other keys stay."""
        try:
            client.auth.admin.update_user_by_id(user_id, {"app_metadata": {"role": role}})
        except AuthError as exc:
            logger.error(f"admin revoke access: Supabase Auth failed: {type(exc).__name__}")
            raise AdminUpstreamError(AUTH_SERVICE_UNAVAILABLE)
