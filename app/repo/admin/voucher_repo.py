"""voucher table for the admin API (MS-5089)."""
from typing import Dict, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

_STATUS_ACTIVE = {"active": True, "inactive": False}


class VoucherAdminRepo(AdminBaseRepository):
    # voucher_code_lower_key: migrations/admin/020_constraints.sql (case-insensitive);
    # voucher_code_key: the original case-sensitive unique constraint (supabase_ddl.sql).
    UNIQUE_CONFLICTS = {
        "voucher_code_lower_key": "duplicate_code",
        "voucher_code_key": "duplicate_code",
    }

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_VOUCHER)

    def search(self, search: Optional[str], status: str, page: PageParams) -> Paged:
        """One page of vouchers whose code contains ``search``, filtered by status, newest first."""
        query = self.filters(self.q(), ilike={"code": search}, eq={"is_active": _STATUS_ACTIVE.get(status)})
        return self.paged(query, page)

    def facets(self, search: Optional[str]) -> Dict[str, int]:
        """Status tab counts over the searched set (the status filter itself is not applied)."""
        total = self.count(ilike={"code": search})
        active = self.count(ilike={"code": search}, eq={"is_active": True})
        return {"all": total, "active": active, "inactive": max(total - active, 0)}
