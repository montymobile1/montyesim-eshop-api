"""user_order for the admin Orders page (MS-5081)."""
from typing import Any, Dict, List, Optional, Tuple

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

#: The Orders page has never listed wallet top-ups; it shows bundle orders only.
EXCLUDED_ORDER_TYPE = "Wallet_Top_Up"
#: Columns never sent to the portal (one-time payment codes).
HIDDEN_COLUMNS = ("otp",)


class UserOrderAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_ORDER)

    def search(self, page: PageParams, *, user_id: Optional[str] = None, status: Optional[str] = None,
               sort: str = "created_at", descending: bool = True) -> Paged:
        """One page of orders, wallet top-ups excluded. ``sort`` is a column name chosen by the
        service from a closed list; ties are broken by ``created_at desc, id desc``."""
        query = self.filters(self.q(), eq={"user_id": user_id, "order_status": status},
                             neq={"order_type": EXCLUDED_ORDER_TYPE})
        order: List[Tuple[str, bool]] = [(sort, descending)]
        if sort != "created_at":
            order.append(("created_at", True))
        order.append(("id", True))
        result = self.paged(query, page, order)
        return Paged(rows=[_public(row) for row in result.rows], total=result.total)


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if key not in HIDDEN_COLUMNS}
