"""Users page data for the admin API (MS-5079).

- ``UserAdminRepo``: the users list and detail through ``admin_search_users`` (users_copy joined
  with auth.users in SQL, migrations/admin/080_users.sql), the wallet transactions through
  ``admin_wallet_transactions`` (same file) and the referrals through
  ``admin_referral_transactions`` (owned by the Referrals story, migrations/admin/090_wallet_referrals.sql);
- ``UserWalletAdminRepo``: the wallet row of one user (amount and currency).
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams


@dataclass(frozen=True)
class UserSearch:
    rows: List[Dict[str, Any]]
    total: int
    facets: Dict[str, int] = field(default_factory=dict)


def _unwrap(result: Any) -> Dict[str, Any]:
    """A scalar jsonb RPC result can arrive wrapped in a list."""
    if isinstance(result, list):
        result = result[0] if result else {}
    return result if isinstance(result, dict) else {}


def _paged(result: Any) -> Paged:
    """``Paged`` from ``{data|items, total|total_count}`` (both spellings of the plan are accepted)."""
    body = _unwrap(result)
    rows = body.get("data")
    if rows is None:
        rows = body.get("items")
    total = body.get("total")
    if total is None:
        total = body.get("total_count")
    return Paged(rows=list(rows or []), total=int(total or 0))


class UserAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_COPY)

    def search(self, search: Optional[str], ban_status: str, page: PageParams) -> UserSearch:
        """One page of users with their ban state, newest first, plus ``facets:{all,banned,active}``
        over the searched set (the ban filter itself is not applied to the facets)."""
        body = _unwrap(self.rpc("admin_search_users", {
            "p_search": search.strip() if search and search.strip() else None,
            "p_ban_status": ban_status,
            "p_limit": page.limit,
            "p_offset": page.offset,
            "p_id": None,
        }))
        return UserSearch(rows=list(body.get("data") or []), total=int(body.get("total") or 0),
                          facets={k: int(v or 0) for k, v in (body.get("facets") or {}).items()})

    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        """One user row (same shape as the list) or ``None``."""
        body = _unwrap(self.rpc("admin_search_users", {
            "p_search": None, "p_ban_status": "all", "p_limit": 1, "p_offset": 0, "p_id": str(user_id),
        }))
        rows = body.get("data") or []
        return rows[0] if rows else None

    def wallet_transactions(self, user_id: str, page: PageParams) -> Paged:
        """One page of the user's wallet transactions with the true total, newest first."""
        return _paged(self.rpc("admin_wallet_transactions", {
            "p_user_id": str(user_id), "p_limit": page.limit, "p_offset": page.offset,
        }))

    def referrals(self, user_id: str, page: PageParams, *, search: Optional[str] = None,
                  from_email: Optional[str] = None, referral_date: Optional[date] = None) -> Paged:
        """The user's referral transactions through ``admin_referral_transactions`` (Referrals story),
        called by name with the parameters the plan describes."""
        return _paged(self.rpc("admin_referral_transactions", {
            "p_user_id": str(user_id),
            "p_search": search.strip() if search and search.strip() else None,
            "p_from_email": from_email.strip() if from_email and from_email.strip() else None,
            "p_date": referral_date.isoformat() if referral_date else None,
            "p_limit": page.limit,
            "p_offset": page.offset,
        }))


class UserWalletAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET)

    def for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """``{amount, currency}`` of the user's wallet, or ``None`` when the user has none."""
        query = self.filters(self.table.select("amount,currency"), eq={"user_id": str(user_id)}).limit(1)
        rows = self._run(lambda: query.execute()).data or []
        if not rows:
            return None
        row = rows[0]
        return {"amount": float(row.get("amount") or 0), "currency": row.get("currency")}
