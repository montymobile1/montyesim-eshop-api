"""Admin API: orders. Owned by MS-5081."""
from typing import Optional
from uuid import UUID

from fastapi import Depends, Query

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import PageParams, ok_page, page_params
from app.schemas.admin.orders import AdminOrderSort, AdminOrderStatus, AdminSortDirection
from app.services.admin.order_service import OrderAdminService

router = admin_router()
service = OrderAdminService()


@router.get("", summary="List orders (wallet top-ups excluded) with their user")
def list_orders(page: PageParams = Depends(page_params),
                user_id: Optional[UUID] = Query(None),
                status: Optional[AdminOrderStatus] = Query(None),
                sort: AdminOrderSort = Query(AdminOrderSort.created_at),
                direction: AdminSortDirection = Query(AdminSortDirection.desc),
                user: AdminUser = Depends(require_permission("admin.orders.read"))):
    return ok_page(service.list(page, user_id=str(user_id) if user_id else None, status=status,
                                sort=sort, direction=direction))
