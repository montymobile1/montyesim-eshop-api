"""Devices list for the admin portal (MS-5081, TEOS-115)."""
from typing import Any, Optional

from app.repo.admin.device_repo import DeviceAdminRepo
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.devices import DeviceLoggedIn, DevicePlatform, DeviceSort
from app.schemas.admin.orders import AdminSortDirection


def _value(item: Any) -> str:
    return item.value if hasattr(item, "value") else str(item)


class DeviceAdminService:
    def __init__(self, devices: Optional[DeviceAdminRepo] = None, users: Optional[UsersCopyAdminRepo] = None):
        self.devices = devices or DeviceAdminRepo()
        self._users = users

    @property
    def users(self) -> UsersCopyAdminRepo:
        if self._users is None:
            self._users = UsersCopyAdminRepo()
        return self._users

    def list(self, page: PageParams, *, user_id: Optional[str] = None,
             platform: DevicePlatform = DevicePlatform.all, logged_in: DeviceLoggedIn = DeviceLoggedIn.all,
             sort: DeviceSort = DeviceSort.created_at,
             direction: AdminSortDirection = AdminSortDirection.desc) -> AdminPage:
        """Devices, each with ``user_email`` (``None`` for an anonymous device or a user no longer in
        users_copy), and ``facets:{all, android, ios}`` over the devices matching ``user_id`` and
        ``logged_in`` (the platform filter itself is not applied to the facets)."""
        platform, logged_in = _value(platform), _value(logged_in)
        result = self.devices.search(page, user_id=user_id, platform=platform, logged_in=logged_in,
                                     sort=_value(sort), descending=_value(direction) == "desc")
        user_ids = [row.get("user_id") for row in result.rows if row.get("user_id")]
        users = self.users.by_ids(user_ids) if user_ids else {}
        rows = [{**row, "user_email": (users.get(str(row.get("user_id"))) or {}).get("email")}
                for row in result.rows]
        facets = self.devices.platform_facets(user_id=user_id, logged_in=logged_in)
        return AdminPage(data=rows, total_count=result.total, facets=facets)
