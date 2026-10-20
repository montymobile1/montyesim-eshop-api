"""Orders list for the admin portal (MS-5081)."""
from typing import Optional

from app.repo.admin.user_order_repo import UserOrderAdminRepo
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.orders import AdminOrderSort, AdminOrderStatus, AdminSortDirection


class OrderAdminService:
    def __init__(self, orders: Optional[UserOrderAdminRepo] = None, users: Optional[UsersCopyAdminRepo] = None):
        self.orders = orders or UserOrderAdminRepo()
        self.users = users or UsersCopyAdminRepo()

    def list(self, page: PageParams, *, user_id: Optional[str] = None,
             status: Optional[AdminOrderStatus] = None,
             sort: AdminOrderSort = AdminOrderSort.created_at,
             direction: AdminSortDirection = AdminSortDirection.desc) -> AdminPage:
        """Orders (wallet top-ups excluded), each with ``user: {id, email, msisdn}`` or ``None``
        when the user no longer exists in users_copy."""
        result = self.orders.search(
            page, user_id=user_id, status=status.value if status else None,
            sort=sort.value, descending=direction == AdminSortDirection.desc,
        )
        users = self.users.by_ids(row.get("user_id") for row in result.rows) if result.rows else {}
        rows = [{**row, "user": users.get(str(row.get("user_id")))} for row in result.rows]
        return AdminPage(data=rows, total_count=result.total)
