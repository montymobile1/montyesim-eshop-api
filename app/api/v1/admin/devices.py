"""Admin API: devices. Owned by MS-5081 (TEOS-115)."""
from typing import Optional
from uuid import UUID

from fastapi import Depends, Query

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import PageParams, ok_page, page_params
from app.schemas.admin.devices import DeviceLoggedIn, DevicePlatform, DeviceSort
from app.schemas.admin.orders import AdminSortDirection
from app.services.admin.device_service import DeviceAdminService

router = admin_router()
service = DeviceAdminService()


@router.get("", summary="List devices with the owner's email and platform facets")
def list_devices(page: PageParams = Depends(page_params),
                 user_id: Optional[UUID] = Query(None),
                 platform: DevicePlatform = Query(DevicePlatform.all),
                 logged_in: DeviceLoggedIn = Query(DeviceLoggedIn.all),
                 sort: DeviceSort = Query(DeviceSort.created_at),
                 direction: AdminSortDirection = Query(AdminSortDirection.desc, alias="dir"),
                 user: AdminUser = Depends(require_permission("admin.devices.read"))):
    """Never returns `fcm_token` or `originated_ip`. `facets:{all,android,ios}` count the devices
    matching `user_id` and `logged_in` (not `platform`). Any other filter, sort or dir value -> 422."""
    return ok_page(service.list(page, user_id=str(user_id) if user_id else None, platform=platform,
                                logged_in=logged_in, sort=sort, direction=direction))
