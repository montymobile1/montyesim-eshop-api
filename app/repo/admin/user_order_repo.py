"""user_order for the admin Orders page (MS-5081, TEOS-117)."""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams
from app.schemas.admin.orders import FACETS

#: The Orders page has never listed wallet top-ups; it shows bundle orders only.
EXCLUDED_ORDER_TYPE = "Wallet_Top_Up"
#: Columns never sent to the portal: one-time payment codes, the payment intent and the raw bundle
#: snapshot (TEOS-117). ``bundle_name`` and ``bundle_code`` are extracted from the snapshot instead.
HIDDEN_COLUMNS = ("otp", "payment_intent_code", "bundle_data")
#: Columns ``search`` matches with ilike (``bundle_id`` holds the bundle code).
SEARCH_COLUMNS = ("promo_code", "bundle_id")


class UserOrderAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_ORDER)

    def search(self, page: PageParams, *, user_id: Optional[str] = None, status: Optional[str] = None,
               payment_status: Optional[str] = None, created_from: Optional[datetime] = None,
               created_to: Optional[datetime] = None, search: Optional[str] = None,
               sort: str = "created_at", descending: bool = True) -> Paged:
        """One page of orders, wallet top-ups excluded. ``status`` / ``payment_status`` are stored
        values; ``created_from`` is inclusive and ``created_to`` exclusive; ``search`` see
        ``_matching``. ``sort`` is a column name chosen by the service from a closed list; ties are
        broken by ``created_at desc, id desc``."""
        query = self._filtered(self.q(), user_id=user_id, payment_status=payment_status,
                               created_from=created_from, created_to=created_to, search=search)
        query = self.filters(query, eq={"order_status": status})
        order: List[Tuple[str, bool]] = [(sort, descending)]
        if sort != "created_at":
            order.append(("created_at", True))
        order.append(("id", True))
        result = self.paged(query, page, order)
        return Paged(rows=[_public(row) for row in result.rows], total=result.total)

    def facets(self, *, user_id: Optional[str] = None, payment_status: Optional[str] = None,
               created_from: Optional[datetime] = None, created_to: Optional[datetime] = None,
               search: Optional[str] = None) -> Dict[str, int]:
        """Status tab counts over the same filters as ``search`` minus the status filter itself.
        One ``head`` count per tab; no rows are transferred."""
        counts: Dict[str, int] = {}
        for key, stored in FACETS:
            query = self._filtered(self.q("id", count=True, head=True), user_id=user_id,
                                   payment_status=payment_status, created_from=created_from,
                                   created_to=created_to, search=search)
            query = self.filters(query, eq={"order_status": stored})
            response = self._run(lambda q=query: q.execute())
            counts[key] = int(response.count or 0)
        return counts

    def _filtered(self, query, *, user_id: Optional[str], payment_status: Optional[str],
                  created_from: Optional[datetime], created_to: Optional[datetime], search: Optional[str]):
        query = self.filters(query, eq={"user_id": user_id, "payment_status": payment_status},
                             neq={"order_type": EXCLUDED_ORDER_TYPE},
                             gte={"created_at": created_from.isoformat() if created_from else None})
        if created_to is not None:
            query = query.lt("created_at", created_to.isoformat())
        return self._matching(query, search)

    def _matching(self, query, term: Optional[str]):
        """``term`` matched literally (case-insensitive) in the promo code or the bundle code
        (``bundle_id`` holds the bundle code), or equal to the order id when it is a UUID (``id`` is a
        uuid column, so a partial id cannot be matched with ilike)."""
        if term is None or not str(term).strip():
            return query
        term = str(term).strip()
        pattern = self.quoted(self.like(term))
        conditions = [f"{column}.ilike.{pattern}" for column in SEARCH_COLUMNS]
        try:
            conditions.append(f"id.eq.{UUID(term)}")
        except ValueError:
            pass
        return query.or_(",".join(conditions))


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    bundle = _bundle(row.get("bundle_data"))
    public = {key: value for key, value in row.items() if key not in HIDDEN_COLUMNS}
    public["bundle_name"] = _text(bundle.get("display_title"))
    public["bundle_code"] = _text(bundle.get("bundle_code")) or _text(row.get("bundle_id"))
    return public


def _bundle(raw: Any) -> Dict[str, Any]:
    """The bundle snapshot as a dict. ``user_order.bundle_data`` is a text column holding JSON (or
    ``"-"``); a dict is accepted too. Anything missing or unreadable gives ``{}``."""
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str) or raw == "-":
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: Any) -> Optional[str]:
    """Same rule as the portal's former ``value || "N/A"``: empty values become ``None``."""
    if not value and not isinstance(value, (list, dict)):  # None, "", 0, False (falsy in JS too)
        return None
    return str(value)
