"""promotion_usage for the admin API (MS-5054 promo analysis). Read only."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository
from app.schemas.admin.common import PageParams


@dataclass(frozen=True)
class UsageSearch:
    """One page of usage rows (joined in SQL), the total and the totals over every matching row."""
    rows: List[Dict[str, Any]]
    total: int
    totals: Dict[str, Any] = field(default_factory=dict)


class PromotionUsageAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION_USAGE)

    def search(self, page: PageParams, *, code: Optional[str] = None, user_id: Optional[str] = None,
               bundle_ids: Optional[List[str]] = None, created_from: Optional[datetime] = None,
               created_to: Optional[datetime] = None) -> UsageSearch:
        """One page through ``admin_promotion_usage`` (supabase_ddl.sql, section 100_promotions.sql), ordered
        ``created_at desc, id desc``. ``code`` is matched literally; ``created_to`` is exclusive."""
        result = self.rpc("admin_promotion_usage", {
            "p_code": code,
            "p_user_id": user_id,
            "p_bundle_ids": [str(b) for b in bundle_ids] if bundle_ids else None,
            "p_from": created_from.isoformat() if created_from else None,
            "p_to": created_to.isoformat() if created_to else None,
            "p_limit": page.limit,
            "p_offset": page.offset,
        }) or {}
        if isinstance(result, list):
            result = result[0] if result else {}
        return UsageSearch(rows=list(result.get("data") or []), total=int(result.get("total") or 0),
                           totals=dict(result.get("totals") or {}))
