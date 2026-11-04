"""Admin API: the Admins page (TEOS-307). Every route needs ``admin.security.manage`` (super admin only)."""
from typing import Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.admins import (AdminAccessRevoked, AdminAccountRow, AdminPasswordResetSent,
                                      AdminRevokeAccess, AdminRoleFilter)
from app.schemas.admin.common import AdminPage, PageParams, ok, ok_page, page_params
from app.schemas.response import Response
from app.services.admin.admin_account_service import AdminAccountService
from app.services.admin.audit_service import audit_context

router = admin_router()

MANAGE = "admin.security.manage"

service = AdminAccountService()

AdminId = Path(..., description="admin account id (uuid)")


@router.get("", response_model=Response[AdminPage[AdminAccountRow]])
def list_admins(search: Optional[str] = Query(None, max_length=100, description="part of the email"),
                role: AdminRoleFilter = Query("all"),
                page: PageParams = Depends(page_params),
                user: AdminUser = Depends(require_permission(MANAGE))) -> Response:
    """Admin accounts (super admins first, then by email) with `facets:{all,super_admin,admin}` over the
    searched set. `is_current_user` marks the caller's own row."""
    return ok_page(service.list(search, role, page, user.id))


@router.post("/{admin_id}/reset-password", response_model=Response[AdminPasswordResetSent])
def send_password_reset(request: Request, admin_id: UUID = AdminId,
                        user: AdminUser = Depends(require_permission(MANAGE))) -> Response:
    """Emails the admin a single-use password reset link (Supabase Auth). The password and the sessions
    only change when the admin sets the new password. 404 `admin_not_found`; 409 `cannot_target_self`;
    429 email rate limit; 502 `RESET_EMAIL_FAILED` / `PASSWORD_RESET_URL_NOT_SET`."""
    return ok(service.send_password_reset(str(admin_id), audit_context(user, request)))


@router.post("/{admin_id}/revoke-access", response_model=Response[AdminAccessRevoked])
def revoke_access(body: AdminRevokeAccess, request: Request, admin_id: UUID = AdminId,
                  user: AdminUser = Depends(require_permission(MANAGE))) -> Response:
    """Removes the admin role (the account is kept) and signs the account out everywhere, audited with the
    reason. 404 `admin_not_found`; 409 `cannot_target_self`; 502 when Supabase Auth fails or the audit
    row cannot be written (the role is put back)."""
    return ok(service.revoke_access(str(admin_id), audit_context(user, request, body.reason)))
