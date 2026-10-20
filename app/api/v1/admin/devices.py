"""Admin API: devices. Owned by MS-5081."""
from typing import Optional
from uuid import UUID

from fastapi import Depends, Query

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import PageParams, ok_page, page_params
from app.services.admin.device_service import DeviceAdminService

router = admin_router()
service = DeviceAdminService()


@router.get("", summary="List devices, newest first")
def list_devices(page: PageParams = Depends(page_params),
                 user_id: Optional[UUID] = Query(None),
                 user: AdminUser = Depends(require_permission("admin.devices.read"))):
    return ok_page(service.list(page, user_id=str(user_id) if user_id else None))
