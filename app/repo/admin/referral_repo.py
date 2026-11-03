"""Referral transactions for the admin API (MS-5056): user_order rows paid with a referral code,
joined to promotion_usage and users_copy by the ``admin_referral_transactions`` SQL function
(supabase_ddl.sql, section 090_wallet_referrals.sql). Read only."""
from typing import Any, Dict, List

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.referrals import AdminReferralFilters

#: Largest p_limit the SQL function accepts (the export reads chunks of this size).
MAX_RPC_LIMIT = 1000


class ReferralAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_ORDER)

    def search(self, filters: AdminReferralFilters, limit: int, offset: int) -> Paged:
        """``limit`` rows from ``offset`` (ordered ``created_at desc, id desc``) and the total count.
        The same function serves the list (one page) and the export (chunks of ``MAX_RPC_LIMIT``);
        the Users story calls it too for ``GET /users/{id}/referrals``."""
        result = self.rpc("admin_referral_transactions", {
            **filters.rpc_params(),
            "p_limit": limit,
            "p_offset": offset,
        }) or {}
        if isinstance(result, list):  # a scalar jsonb RPC result can arrive wrapped in a list
            result = result[0] if result else {}
        rows: List[Dict[str, Any]] = list(result.get("data") or [])
        return Paged(rows=rows, total=int(result.get("total") or 0))
