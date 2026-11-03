"""users_copy (a copy of auth.users: id, email, metadata) for the admin API.

Shared by several stories to show who a row belongs to or who made a change:
- ``by_ids``: owned by the foundation, used to join users server-side (orders, vouchers, audit log...);
- ``lookup``: user dropdowns, added by MS-5077.
"""
from typing import Any, Dict, Iterable, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

MAX_IDS_PER_QUERY = 200


def user_summary(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """``{id, email, msisdn}`` from a users_copy row (msisdn lives in metadata)."""
    metadata = row.get("metadata") or {}
    return {"id": str(row.get("id")), "email": row.get("email"), "msisdn": metadata.get("msisdn")}


class UsersCopyAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_COPY)

    def by_ids(self, ids: Iterable[Any]) -> Dict[str, Dict[str, Optional[str]]]:
        """``{user_id: {id, email, msisdn}}`` for the given ids; unknown ids are absent. ``None`` ids are skipped."""
        unique = sorted({str(i) for i in ids if i})
        found: Dict[str, Dict[str, Optional[str]]] = {}
        for start in range(0, len(unique), MAX_IDS_PER_QUERY):
            chunk = unique[start:start + MAX_IDS_PER_QUERY]
            query = self.filters(self.table.select("id,email,metadata"), in_={"id": chunk})
            rows = self._run(lambda: query.execute()).data or []
            for row in rows:
                summary = user_summary(row)
                found[summary["id"]] = summary
        return found

    # ---------- MS-5077: user dropdowns (orders, devices, promo analysis, referrals) ----------

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, email}`` rows that have an email, matching ``search`` on email, ordered by email, id."""
        query = self.filters(self.q("id,email"), is_null={"email": False}, ilike={"email": search},
                             eq={"id": record_id})
        return self.paged(query, page, order=(("email", False), ("id", False)))
