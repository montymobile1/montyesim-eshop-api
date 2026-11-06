"""Admin API: the signed-in admin's own trusted devices (TEOS-255 / TEOS-256), mounted at /me.

Every admin, no permission key (contract D6). The caller's browser is the row of the caller's Supabase
session (``AdminUser.session_id``, set by the per-request device check).
"""
from typing import Literal, Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, admin_user
from app.schemas.admin.common import ok, ok_page
from app.schemas.admin.trusted_devices import (AdminDeviceList, AdminDeviceRenameRequest, AdminDeviceRow,
                                               AdminDevicesRevoked)
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.trusted_device_service import (REASON_SIGN_OUT_OTHERS, REASON_USER,
                                                       TrustedDeviceService)

router = admin_router()

service = TrustedDeviceService()

DeviceId = Path(..., description="device id (uuid)")


def session_of(user: AdminUser) -> Optional[str]:
    return getattr(user, "session_id", None)


def current_device_id(user: AdminUser) -> Optional[str]:
    """The caller's device row id: from the request's device check, else looked up by session."""
    device_id = getattr(user, "device_id", None)
    if device_id:
        return str(device_id)
    session_id = session_of(user)
    row = service.session_device(session_id) if session_id else None
    return str(row["id"]) if row and not row.get("revoked_at") else None


@router.get("/devices", response_model=Response[AdminDeviceList])
def list_my_devices(user: AdminUser = Depends(admin_user)) -> Response:
    """The caller's active devices (`last_used_at` desc, max 100). `current` marks this browser."""
    rows = service.list_devices(user.id, session_of(user))
    return ok_page(AdminDeviceList(data=rows, total_count=len(rows)))


@router.patch("/devices/{device_id}", response_model=Response[AdminDeviceRow])
def rename_my_device(body: AdminDeviceRenameRequest, request: Request, device_id: UUID = DeviceId,
                     user: AdminUser = Depends(admin_user)) -> Response:
    """`name` 1..60 characters (spaces collapsed). 404 `device_not_found`."""
    return ok(service.rename(user.id, str(device_id), body.name, audit_context(user, request),
                             current_session_id=session_of(user)))


@router.delete("/devices/{device_id}", status_code=204, response_class=HttpResponse)
def revoke_my_device(request: Request, device_id: UUID = DeviceId,
                     user: AdminUser = Depends(admin_user)) -> HttpResponse:
    """204: the device's session is ended (its next request and refresh get 401 `device_revoked`). The
    current device is allowed (the portal then signs out). 404 `device_not_found`."""
    service.revoke(user.id, str(device_id), REASON_USER, audit_context(user, request))
    return HttpResponse(status_code=204)


@router.delete("/devices", response_model=Response[AdminDevicesRevoked])
def revoke_my_other_devices(request: Request,
                            except_: Literal["current"] = Query(..., alias="except",
                                                                description="only `current`"),
                            user: AdminUser = Depends(admin_user)) -> Response:
    """Signs out every device of the caller except this browser. `except=current` is required (422
    without it). Answers `{revoked: n}`."""
    count = service.revoke_all(user.id, REASON_SIGN_OUT_OTHERS, audit_context(user, request),
                               except_device_id=current_device_id(user))
    return ok(AdminDevicesRevoked(revoked=count))
