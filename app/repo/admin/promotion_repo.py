"""promotion table for the admin API (MS-5050 list, MS-5052 add/edit).

The list goes through ``admin_search_promotions`` (migrations/admin/100_promotions.sql): status tabs,
literal search on name or code, rule and bundle filters, the CSV ``bundle_code`` split server-side and
the rule embedded. Single reads use a PostgREST embed with the same nested keys, so the service shapes
both the same way. Writes go through the audited SQL helpers.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository
from app.schemas.admin.common import PageParams

#: Columns returned by a single read: everything but callback_url / callback_headers, plus the rule.
PROMOTION_SELECT = ("id,name,code,type,amount,valid_from,valid_to,is_active,times_used,created_at,rule_id,"
                    "bundle_code,"
                    "promotion_rule(id,name,max_usage,beneficiary,promotion_rule_action(id,name),"
                    "promotion_rule_event(id,name))")


@dataclass(frozen=True)
class PromotionSearch:
    rows: List[Dict[str, Any]]
    total: int
    facets: Dict[str, int] = field(default_factory=dict)


class PromotionAdminRepo(AdminBaseRepository):
    # promotion_code_key / promotion_name_key: supabase_ddl.sql (also re-checked by 020_constraints.sql)
    UNIQUE_CONFLICTS = {
        "promotion_code_key": "duplicate_code",
        "promotion_name_key": "duplicate_name",
    }

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION)

    def search(self, page: PageParams, *, search: Optional[str] = None, status: str = "all",
               rule_ids: Optional[List[str]] = None, bundle_codes: Optional[List[str]] = None,
               include_global: bool = False) -> PromotionSearch:
        """One page through ``admin_search_promotions``; ``facets`` count the searched set per tab."""
        result = self.rpc("admin_search_promotions", {
            "p_search": search,
            "p_status": status,
            "p_rule_ids": [str(r) for r in rule_ids] if rule_ids else None,
            "p_bundle_codes": list(bundle_codes) if bundle_codes else None,
            "p_include_global": bool(include_global),
            "p_limit": page.limit,
            "p_offset": page.offset,
        }) or {}
        if isinstance(result, list):  # a scalar jsonb RPC result can arrive wrapped in a list
            result = result[0] if result else {}
        return PromotionSearch(rows=list(result.get("data") or []), total=int(result.get("total") or 0),
                               facets={str(k): int(v) for k, v in (result.get("facets") or {}).items()})

    def get(self, promotion_id: Any) -> Optional[Dict[str, Any]]:
        """One promotion with its rule, action and event, or None."""
        return self.one(promotion_id, PROMOTION_SELECT)
