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
    users.by_ids.return_value = {"u1": U1}

    page = OrderAdminService(orders, users).list(PAGE)

    assert page.total_count == 7
    assert page.data == [{"id": "o1", "user_id": "u1", "user": U1}, {"id": "o2", "user_id": "gone", "user": None}]
    assert list(users.by_ids.call_args.args[0]) == ["u1", "gone"]
    orders.search.assert_called_once_with(PAGE, user_id=None, status=None, sort="created_at", descending=True)


def test_orders_pass_enums_as_plain_values():
    orders, users = MagicMock(), MagicMock()
    orders.search.return_value = Paged(rows=[], total=0)

    page = OrderAdminService(orders, users).list(PAGE, user_id="u1", status=AdminOrderStatus.failure,
                                                 sort=AdminOrderSort.amount, direction=AdminSortDirection.asc)

    orders.search.assert_called_once_with(PAGE, user_id="u1", status="failure", sort="amount", descending=False)
    users.by_ids.assert_not_called()
    assert page.data == [] and page.total_count == 0


def test_devices_list():
    devices = MagicMock()
    devices.search.return_value = Paged(rows=[{"id": 1}], total=1)

    page = DeviceAdminService(devices).list(PAGE, user_id="u1")

    devices.search.assert_called_once_with(PAGE, user_id="u1")
    assert page.data == [{"id": 1}] and page.total_count == 1


def test_contact_us_list():
    messages = MagicMock()
    messages.search.return_value = Paged(rows=[{"id": 3, "email": "a@x.com"}], total=1)

    page = ContactUsAdminService(messages).list(PAGE, search="a@")

    messages.search.assert_called_once_with(PAGE, search="a@")
    assert page.total_count == 1
