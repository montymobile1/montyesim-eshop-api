"""MS-5081 services: orders user join, devices and contact-us pass-through."""
from unittest.mock import MagicMock

from app.repo.admin.base import Paged
from app.schemas.admin.common import PageParams
from app.schemas.admin.orders import AdminOrderSort, AdminOrderStatus, AdminSortDirection
from app.services.admin.contact_us_service import ContactUsAdminService
from app.services.admin.device_service import DeviceAdminService
from app.services.admin.order_service import OrderAdminService

PAGE = PageParams(page=1, page_size=20)
U1 = {"id": "u1", "email": "a@x.com", "msisdn": "+9611"}


def test_orders_join_users_and_null_for_deleted_users():
    orders, users = MagicMock(), MagicMock()
    orders.search.return_value = Paged(rows=[{"id": "o1", "user_id": "u1"}, {"id": "o2", "user_id": "gone"}], total=7)
    orders.facets.return_value = {}
    users.by_ids.return_value = {"u1": U1}

    page = OrderAdminService(orders, users).list(PAGE)

    assert page.total_count == 7
    assert page.data == [{"id": "o1", "user_id": "u1", "user": U1}, {"id": "o2", "user_id": "gone", "user": None}]
    assert list(users.by_ids.call_args.args[0]) == ["u1", "gone"]
    orders.search.assert_called_once_with(PAGE, user_id=None, status=None, payment_status=None, created_from=None,
                                          created_to=None, search=None, sort="created_at", descending=True)


def test_orders_pass_enums_as_plain_values():
    orders, users = MagicMock(), MagicMock()
    orders.search.return_value = Paged(rows=[], total=0)
    orders.facets.return_value = {}

    page = OrderAdminService(orders, users).list(PAGE, user_id="u1", status=AdminOrderStatus.failure,
                                                 sort=AdminOrderSort.amount, direction=AdminSortDirection.asc)

    orders.search.assert_called_once_with(PAGE, user_id="u1", status="failure", payment_status=None,
                                          created_from=None, created_to=None, search=None, sort="amount",
                                          descending=False)
    users.by_ids.assert_not_called()
    assert page.data == [] and page.total_count == 0


def test_devices_list():
    devices, users = MagicMock(), MagicMock()
    devices.search.return_value = Paged(rows=[{"id": 1, "user_id": "u1"}, {"id": 2, "user_id": None},
                                              {"id": 3, "user_id": "gone"}], total=3)
    devices.platform_facets.return_value = {"all": 3, "android": 1, "ios": 2}
    users.by_ids.return_value = {"u1": U1}

    page = DeviceAdminService(devices, users).list(PAGE, user_id="u1")

    devices.search.assert_called_once_with(PAGE, user_id="u1", platform="all", logged_in="all",
                                           sort="created_at", descending=True)
    devices.platform_facets.assert_called_once_with(user_id="u1", logged_in="all")
    users.by_ids.assert_called_once_with(["u1", "gone"])
    assert [row["user_email"] for row in page.data] == ["a@x.com", None, None]
    assert page.total_count == 3 and page.facets == {"all": 3, "android": 1, "ios": 2}


def test_contact_us_list():
    messages = MagicMock()
    messages.search.return_value = Paged(rows=[{"id": 3, "email": "a@x.com"}], total=1)

    page = ContactUsAdminService(messages).list(PAGE, search="a@")

    messages.search.assert_called_once_with(PAGE, search="a@", created_from=None, created_to=None)
    assert page.total_count == 1


def test_contact_us_dates_become_a_half_open_range():
    from datetime import date, datetime
    messages = MagicMock()
    messages.search.return_value = Paged(rows=[], total=0)

    page = ContactUsAdminService(messages).list(PAGE, search="hi", date_from=date(2026, 10, 1),
                                                date_to=date(2026, 10, 21))

    messages.search.assert_called_once_with(PAGE, search="hi", created_from=datetime(2026, 10, 1),
                                            created_to=datetime(2026, 10, 22))
    assert page.data == [] and page.total_count == 0


def test_contact_us_same_day_range_is_allowed():
    from datetime import date
    messages = MagicMock()
    messages.search.return_value = Paged(rows=[], total=0)

    ContactUsAdminService(messages).list(PAGE, date_from=date(2026, 10, 1), date_to=date(2026, 10, 1))

    messages.search.assert_called_once()


def test_contact_us_from_after_to_is_rejected_before_the_query():
    from datetime import date
    import pytest
    from app.exceptions.admin import AdminValidationError
    messages = MagicMock()

    with pytest.raises(AdminValidationError):
        ContactUsAdminService(messages).list(PAGE, date_from=date(2026, 10, 2), date_to=date(2026, 10, 1))

    messages.search.assert_not_called()


def test_contact_us_rows_returned_as_stored():
    messages = MagicMock()
    row = {"id": 9, "email": "A@X.com", "content": "<b>مرحبا</b>\nline 2 " + "x" * 10000, "created_at": "2026-10-01T00:00:00"}
    messages.search.return_value = Paged(rows=[row], total=41)

    page = ContactUsAdminService(messages).list(PAGE)

    assert page.data == [row] and page.total_count == 41
