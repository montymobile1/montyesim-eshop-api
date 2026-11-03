"""Orders list for the admin portal (MS-5081, TEOS-117)."""
from datetime import date
from typing import Optional

from app.repo.admin.user_order_repo import UserOrderAdminRepo
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.orders import (STORED_STATUS, AdminOrderSort, AdminOrderStatus, AdminPaymentStatus,
                                      AdminSortDirection)
from app.services.admin.promotion_service import day_range


class OrderAdminService:
    def __init__(self, orders: Optional[UserOrderAdminRepo] = None, users: Optional[UsersCopyAdminRepo] = None):
        self.orders = orders or UserOrderAdminRepo()
        self.users = users or UsersCopyAdminRepo()

    def list(self, page: PageParams, *, user_id: Optional[str] = None,
             status: Optional[AdminOrderStatus] = None,
             payment_status: Optional[AdminPaymentStatus] = None,
             date_from: Optional[date] = None, date_to: Optional[date] = None,
             search: Optional[str] = None,
             sort: AdminOrderSort = AdminOrderSort.created_at,
             direction: AdminSortDirection = AdminSortDirection.desc) -> AdminPage:
        """Orders (wallet top-ups excluded), each with ``user: {id, email, msisdn}`` or ``None``
        when the user no longer exists in users_copy, plus ``facets`` (status tab counts over the
        same filters, the status filter excluded). 422 before any query when ``date_from`` is after
        ``date_to`` or the range is over 366 days."""
        start, end = day_range(date_from, date_to)
        filters = dict(user_id=user_id,
                       payment_status=STORED_STATUS[payment_status.value] if payment_status else None,
                       created_from=start, created_to=end,
                       search=search.strip() if search and search.strip() else None)
        result = self.orders.search(
            page, status=STORED_STATUS[status.value] if status else None,
            sort=sort.value, descending=direction == AdminSortDirection.desc, **filters,
        )
        facets = self.orders.facets(**filters)
        users = self.users.by_ids(row.get("user_id") for row in result.rows) if result.rows else {}
        rows = [{**row, "user": users.get(str(row.get("user_id")))} for row in result.rows]
        return AdminPage(data=rows, total_count=result.total, facets=facets)
