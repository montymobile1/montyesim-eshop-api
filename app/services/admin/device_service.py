"""Devices list for the admin portal (MS-5081)."""
from typing import Optional

from app.repo.admin.device_repo import DeviceAdminRepo
from app.schemas.admin.common import AdminPage, PageParams


class DeviceAdminService:
    def __init__(self, devices: Optional[DeviceAdminRepo] = None):
        self.devices = devices or DeviceAdminRepo()

    def list(self, page: PageParams, *, user_id: Optional[str] = None) -> AdminPage:
        result = self.devices.search(page, user_id=user_id)
        return AdminPage(data=result.rows, total_count=result.total)
