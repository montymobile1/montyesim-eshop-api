"""TEOS-117 orders service: status / payment status mapping, date range, search, facets."""
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from app.exceptions.admin import AdminValidationError
from app.repo.admin.base import Paged
from app.schemas.admin.common import PageParams
from app.schemas.admin.orders import AdminOrderSort, AdminOrderStatus, AdminPaymentStatus, AdminSortDirection
from app.services.admin.order_service import OrderAdminService

PAGE = PageParams(page=1, page_size=20)
FACETS = {"all": 3, "success": 1, "pending": 1, "canceled": 0, "failed": 1}


def _service():
    orders, users = MagicMock(), MagicMock()
    orders.search.return_value = Paged(rows=[], total=0)
    orders.facets.return_value = FACETS
    return OrderAdminService(orders, users), orders


def test_filters_reach_search_and_facets_without_the_status():
    service, orders = _service()

    page = service.list(PAGE, user_id="u1", status=AdminOrderStatus.failed,
                        payment_status=AdminPaymentStatus.failed, date_from=date(2026, 1, 1),
                        date_to=date(2026, 1, 31), search="  EU  ", sort=AdminOrderSort.order_status,
                        direction=AdminSortDirection.asc)

    filters = dict(user_id="u1", payment_status="failure", created_from=datetime(2026, 1, 1),
                   created_to=datetime(2026, 2, 1), search="EU")
    orders.search.assert_called_once_with(PAGE, status="failure", sort="order_status", descending=False, **filters)
    orders.facets.assert_called_once_with(**filters)
    assert page.facets == FACETS


@pytest.mark.parametrize("status, stored", [
    (AdminOrderStatus.all, None), (None, None), (AdminOrderStatus.success, "success"),
    (AdminOrderStatus.pending, "pending"), (AdminOrderStatus.canceled, "canceled"),
    (AdminOrderStatus.failed, "failure"), (AdminOrderStatus.failure, "failure"),
])
def test_status_tabs_map_to_stored_values(status, stored):
    service, orders = _service()

    service.list(PAGE, status=status)

    assert orders.search.call_args.kwargs["status"] == stored


def test_blank_search_is_none():
    service, orders = _service()

    service.list(PAGE, search="   ")

    assert orders.search.call_args.kwargs["search"] is None


@pytest.mark.parametrize("date_from, date_to", [
    (date(2026, 2, 1), date(2026, 1, 31)),
    (date(2025, 1, 1), date(2026, 1, 2)),
])
def test_invalid_range_is_rejected_before_any_query(date_from, date_to):
    service, orders = _service()

    with pytest.raises(AdminValidationError):
        service.list(PAGE, date_from=date_from, date_to=date_to)

    orders.search.assert_not_called()
    orders.facets.assert_not_called()


def test_a_366_day_range_is_allowed():
    service, orders = _service()

    service.list(PAGE, date_from=date(2024, 1, 1), date_to=date(2024, 12, 31))  # leap year: 366 days

    orders.search.assert_called_once()
