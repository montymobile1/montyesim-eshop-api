"""Admin API: the Admins page (TEOS-307) and an admin's trusted devices (TEOS-255). Every route needs
``admin.security.manage`` (super admin only)."""
from typing import Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.admins import (AdminAccessRevoked, AdminAccountPage, AdminPasswordResetSent,
                                      AdminRevokeAccess, AdminRoleFilter)
from app.schemas.admin.common import PageParams, ok, ok_page, page_params
from app.schemas.admin.trusted_devices import AdminDeviceList
from app.schemas.response import Response
from app.services.admin.admin_account_service import AdminAccountService
from app.services.admin.audit_service import audit_context
from app.services.admin.trusted_device_service import REASON_SUPER_ADMIN, TrustedDeviceService

router = admin_router()

MANAGE = "admin.security.manage"

service = AdminAccountService()
devices = TrustedDeviceService()

AdminId = Path(..., description="admin account id (uuid)")
DeviceId = Path(..., description="device id (uuid)")


@router.get("", response_model=Response[AdminAccountPage])
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


@router.get("/{admin_id}/devices", response_model=Response[AdminDeviceList])
def list_admin_devices(request: Request, admin_id: UUID = AdminId,
                       user: AdminUser = Depends(require_permission(MANAGE))) -> Response:
    """The admin's active trusted devices (`last_used_at` desc, max 100). `current` is true only for the
    caller's own browser. 404 `admin_not_found` when the id is not an admin account."""
    service.admin_account(str(admin_id))
    rows = devices.list_devices(str(admin_id), getattr(user, "session_id", None))
    return ok_page(AdminDeviceList(data=rows, total_count=len(rows)))


@router.delete("/{admin_id}/devices/{device_id}", status_code=204, response_class=HttpResponse)
def revoke_admin_device(request: Request, admin_id: UUID = AdminId, device_id: UUID = DeviceId,
                        user: AdminUser = Depends(require_permission(MANAGE))) -> HttpResponse:
    """204: the device's session is ended (its next request and refresh get 401 `device_revoked`).
    404 `device_not_found` when it is not an active device of that admin."""
    devices.revoke(str(admin_id), str(device_id), REASON_SUPER_ADMIN, audit_context(user, request))
    return HttpResponse(status_code=204)
