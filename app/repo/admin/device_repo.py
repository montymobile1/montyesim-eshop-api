"""device for the admin Devices page (MS-5081, TEOS-115)."""
from typing import Any, Dict, List, Optional, Tuple

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

#: Columns never sent to the portal: the push-notification token of the phone and the IP the
#: device registered from (TEOS-115 case 16).
HIDDEN_COLUMNS = ("fcm_token", "originated_ip")
#: Column that holds the platform: the app registers the OS name in ``device.os``. It is matched
#: case-insensitively and exactly (``ilike`` without wildcards), so "Android"/"android" and
#: "iOS"/"ios" all count.
PLATFORM_COLUMN = "os"
PLATFORMS = ("android", "ios")
#: Sort columns the repository accepts (the API enum is the allowlist; this guards the repo too).
SORT_COLUMNS = ("created_at", "timestamp_login")
LOGGED_IN = {"yes": True, "no": False}


class DeviceAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_DEVICE)

    def search(self, page: PageParams, *, user_id: Optional[str] = None, platform: str = "all",
               logged_in: str = "all", sort: str = "created_at", descending: bool = True) -> Paged:
        """One page of devices. Default order ``created_at desc, id desc``; another sort is followed
        by ``created_at desc`` and then ``id`` in the same direction, so paging stays stable."""
        if sort not in SORT_COLUMNS:
            raise ValueError(f"invalid sort column: {sort!r}")
        query = self._filtered(self.q(), user_id=user_id, logged_in=logged_in, platform=platform)
        if sort == "created_at" and descending:
            result = self.paged(query, page)
        else:
            order: List[Tuple[str, bool]] = [(sort, descending)]
            if sort != "created_at":
                order.append(("created_at", True))
            order.append(("id", descending))
            result = self.paged(query, page, order)
        return Paged(rows=[_public(row) for row in result.rows], total=result.total)

    def platform_facets(self, *, user_id: Optional[str] = None, logged_in: str = "all") -> Dict[str, int]:
        """``{all, android, ios}`` over the devices matching the other filters. The platform filter
        itself is not applied, so the tab counts stay put when a tab is chosen. No rows are sent."""
        facets = {"all": self._count(user_id=user_id, logged_in=logged_in, platform="all")}
        for platform in PLATFORMS:
            facets[platform] = self._count(user_id=user_id, logged_in=logged_in, platform=platform)
        return facets

    # ---------- helpers ----------

    def _filtered(self, query, *, user_id: Optional[str], logged_in: str, platform: str):
        if platform != "all" and platform not in PLATFORMS:
            raise ValueError(f"invalid platform: {platform!r}")
        if logged_in != "all" and logged_in not in LOGGED_IN:
            raise ValueError(f"invalid logged_in: {logged_in!r}")
        query = self.filters(query, eq={"user_id": user_id, "is_logged_in": LOGGED_IN.get(logged_in)})
        if platform in PLATFORMS:
            # no wildcard: a case-insensitive equality on the same column the facets count
            query = query.ilike(PLATFORM_COLUMN, platform)
        return query

    def _count(self, *, user_id: Optional[str], logged_in: str, platform: str) -> int:
        query = self._filtered(self.q("id", count=True, head=True), user_id=user_id, logged_in=logged_in,
                               platform=platform)
        response = self._run(lambda: query.execute())
        return response.count or 0


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if key not in HIDDEN_COLUMNS}
