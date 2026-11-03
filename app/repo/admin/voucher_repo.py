"""voucher table for the admin API (MS-5089, TEOS-133)."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

_STATUS_ACTIVE = {"active": True, "inactive": False}


def _now() -> str:
    """Now in UTC as a naive ISO timestamp: voucher.expired_at is ``timestamp`` (without time zone)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def status_filters(status: str, now: Optional[str] = None) -> Dict[str, Any]:
    """``filters()`` keyword arguments of one status tab (TEOS-133).

    - all: every voucher; active / inactive: ``is_active``;
    - used: redeemed (``is_used``);
    - expired: not redeemed and ``expired_at`` in the past.
    """
    if status in _STATUS_ACTIVE:
        return {"eq": {"is_active": _STATUS_ACTIVE[status]}}
    if status == "used":
        return {"eq": {"is_used": True}}
    if status == "expired":
        return {"eq": {"is_used": False}, "lte": {"expired_at": now or _now()}}
    return {}


class VoucherAdminRepo(AdminBaseRepository):
    # voucher_code_lower_key: supabase_ddl.sql, section 020_constraints.sql (case-insensitive);
    # voucher_code_key: the original case-sensitive unique constraint (supabase_ddl.sql).
    UNIQUE_CONFLICTS = {
        "voucher_code_lower_key": "duplicate_code",
        "voucher_code_key": "duplicate_code",
    }

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_VOUCHER)

    def search(self, search: Optional[str], status: str, page: PageParams) -> Paged:
        """One page of vouchers whose code contains ``search``, filtered by status, newest first."""
        query = self.filters(self.q(), ilike={"code": search}, **status_filters(status))
        return self.paged(query, page)

    def facets(self, search: Optional[str]) -> Dict[str, int]:
        """Status tab counts over the searched set (the status filter itself is not applied).
        ``active + inactive = all``; ``used`` and ``expired`` are subsets of ``all``."""
        now = _now()
        total = self.count(ilike={"code": search})
        active = self.count(ilike={"code": search}, **status_filters("active"))
        used = self.count(ilike={"code": search}, **status_filters("used"))
        expired = self.count(ilike={"code": search}, **status_filters("expired", now))
        return {"all": total, "active": active, "inactive": max(total - active, 0), "used": used,
                "expired": expired}
