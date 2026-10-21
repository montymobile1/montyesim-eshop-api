"""banner table for the admin API (MS-5058, MS-5060)."""
from typing import Dict, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

_STATUS_ACTIVE = {"active": True, "inactive": False}


class BannerAdminRepo(AdminBaseRepository):
    # banner_title_lower_key: unique index on lower(title), migrations/admin/020_constraints.sql
    UNIQUE_CONFLICTS = {"banner_title_lower_key": "duplicate_title"}

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BANNER)

    def search(self, search: Optional[str], status: str, page: PageParams) -> Paged:
        """One page of banners whose title contains ``search``, filtered by status, newest first."""
        query = self.filters(self.q(), ilike={"title": search}, eq={"is_active": _STATUS_ACTIVE.get(status)})
        return self.paged(query, page)

    def facets(self, search: Optional[str]) -> Dict[str, int]:
        """Status tab counts over the searched set (the status filter itself is not applied)."""
        total = self.count(ilike={"title": search})
        active = self.count(ilike={"title": search}, eq={"is_active": True})
        return {"all": total, "active": active, "inactive": max(total - active, 0)}
