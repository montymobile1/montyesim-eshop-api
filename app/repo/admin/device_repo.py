"""device for the admin Devices page (MS-5081)."""
from typing import Any, Dict, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

#: Columns never sent to the portal (push-notification token of the phone).
HIDDEN_COLUMNS = ("fcm_token",)


class DeviceAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_DEVICE)

    def search(self, page: PageParams, *, user_id: Optional[str] = None) -> Paged:
        """One page of devices, newest first (``created_at desc, id desc``)."""
        query = self.filters(self.q(), eq={"user_id": user_id})
        result = self.paged(query, page)
        return Paged(rows=[_public(row) for row in result.rows], total=result.total)


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if key not in HIDDEN_COLUMNS}
