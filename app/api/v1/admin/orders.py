"""Admin API: orders. Owned by MS-5081, TEOS-117."""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import Depends, Query

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import PageParams, ok_page, page_params
from app.schemas.admin.orders import (SEARCH_MAX, AdminOrderSort, AdminOrderStatus, AdminPaymentStatus,
                                      AdminSortDirection)
from app.services.admin.order_service import OrderAdminService

router = admin_router()
service = OrderAdminService()


@router.get("", summary="List orders (wallet top-ups excluded) with their user and status tab counts")
def list_orders(page: PageParams = Depends(page_params),
                user_id: Optional[UUID] = Query(None),
                status: Optional[AdminOrderStatus] = Query(
                    None, description="all (default) | success | pending | canceled | failed "
                                      "(failure is accepted as a synonym of failed)"),
                payment_status: Optional[AdminPaymentStatus] = Query(None),
                date_from: Optional[date] = Query(None, alias="from", description="First day (UTC), inclusive"),
                date_to: Optional[date] = Query(None, alias="to", description="Last day (UTC), inclusive"),
                search: Optional[str] = Query(
                    None, max_length=SEARCH_MAX,
                    description="Order id (full UUID), bundle code or promo code; case-insensitive, literal"),
                sort: AdminOrderSort = Query(AdminOrderSort.created_at),
                direction: Optional[AdminSortDirection] = Query(None),
                dir_: Optional[AdminSortDirection] = Query(None, alias="dir",
                                                           description="Same as direction (wins when both are sent)"),
                user: AdminUser = Depends(require_permission("admin.orders.read"))):
    """Ties broken by ``created_at desc, id desc``. ``facets``: counts for all / success / pending /
    canceled / failed under the same filters, the status filter excluded. 422: invalid enum, sort,
    date or page, ``from`` after ``to``, a range over 366 days, search over 100 characters, unknown
    parameter. 401 no session, 403 missing permission, 502 database error."""
    return ok_page(service.list(page, user_id=str(user_id) if user_id else None, status=status,
                                payment_status=payment_status, date_from=date_from, date_to=date_to,
                                search=search, sort=sort,
                                direction=dir_ or direction or AdminSortDirection.desc))
