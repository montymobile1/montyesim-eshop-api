"""HTTP contract of GET /api/v1/admin/orders (MS-5081)."""
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminUpstreamError
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.orders import AdminOrderSort, AdminOrderStatus, AdminPaymentStatus, AdminSortDirection
from tests.admin.conftest import make_admin

URL = "/api/v1/admin/orders"
USER_ID = "22222222-2222-2222-2222-222222222222"
ROW = {"id": "o1", "user_id": USER_ID, "amount": 1999, "currency": "EUR",
       "user": {"id": USER_ID, "email": "a@x.com", "msisdn": "+9611"}}


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import orders as routes
    fake = MagicMock()
    fake.list.return_value = AdminPage(data=[ROW], total_count=41)
    monkeypatch.setattr(routes, "service", fake)
    return fake


def test_default_list(api_client, as_admin, service):
    response = api_client.get(URL)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["data"] == [ROW]
    assert body["data"]["total_count"] == 41
    assert body["totalCount"] == 41
    service.list.assert_called_once_with(PageParams(page=1, page_size=20), user_id=None, status=None,
                                         payment_status=None, date_from=None, date_to=None, search=None,
                                         sort=AdminOrderSort.created_at, direction=AdminSortDirection.desc)


def test_filters_sort_and_paging_are_passed(api_client, as_admin, service):
    response = api_client.get(URL, params={"user_id": USER_ID, "status": "canceled", "sort": "amount",
                                           "direction": "asc", "page": 3, "page_size": 50})

    assert response.status_code == 200
    service.list.assert_called_once_with(PageParams(page=3, page_size=50), user_id=USER_ID,
                                         status=AdminOrderStatus.canceled, payment_status=None, date_from=None,
                                         date_to=None, search=None, sort=AdminOrderSort.amount,
                                         direction=AdminSortDirection.asc)


def test_super_admin_can_read(api_client, service):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_SUPER_ADMIN)
    try:
        assert api_client.get(URL).status_code == 200
    finally:
        esim_app.dependency_overrides.pop(admin_user, None)


@pytest.mark.parametrize("params", [
    {"sort": "created_at;drop"},
    {"sort": "bundle_id"},
    {"direction": "sideways"},
    {"status": "foo"},
    {"user_id": "not-a-uuid"},
    {"page_size": 101},
    {"page": 0},
    {"sortBy": "amount"},
    {"sort": "payment_intent_code"},
    {"status": "failures"},
    {"payment_status": "paid"},
    {"dir": "up"},
    {"from": "2026-13-01"},
    {"to": "yesterday"},
    {"search": "x" * 101},
    {"page_size": 0},
])
def test_invalid_query_is_422(api_client, as_admin, service, params):
    response = api_client.get(URL, params=params)

    assert response.status_code == 422
    assert response.json()["responseCode"] == 422
    service.list.assert_not_called()


def test_no_session_is_401(api_client, service):
    response = api_client.get(URL)

    assert response.status_code == 401
    service.list.assert_not_called()


def test_missing_permission_is_403(api_client, service):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="u", email="x@example.com", role=ROLE_ADMIN, permissions=frozenset({"admin.users.read"}))
    try:
        response = api_client.get(URL)
    finally:
        esim_app.dependency_overrides.pop(admin_user, None)

    assert response.status_code == 403
    assert response.json()["developerMessage"] == "admin.orders.read"


def test_database_error_is_502(api_client, as_admin, service):
    service.list.side_effect = AdminUpstreamError("DATABASE_ERROR")

    response = api_client.get(URL)

    assert response.status_code == 502
    assert response.json()["developerMessage"] == "DATABASE_ERROR"


def test_empty_list(api_client, as_admin, service):
    service.list.return_value = AdminPage(data=[], total_count=0)

    body = api_client.get(URL).json()

    assert body["data"]["data"] == []
    assert body["data"]["total_count"] == 0


def test_response_never_contains_payment_intent_or_bundle_data(api_client, as_admin, monkeypatch):
    """TEOS-117 case 17, through the real service and repo (only the database call is replaced)."""
    from app.api.v1.admin import orders as routes
    from app.repo.admin.base import Paged
    from app.repo.admin.user_order_repo import UserOrderAdminRepo
    from app.services.admin.order_service import OrderAdminService

    repo = UserOrderAdminRepo()
    monkeypatch.setattr(repo, "paged", lambda query, page, order=None: Paged(rows=[
        {"id": "o1", "user_id": USER_ID, "amount": 1999, "payment_intent_code": "pi_secret", "otp": "123",
         "bundle_data": '{"display_title": "Europe 5GB", "bundle_code": "EU-5GB"}'},
    ], total=1))
    monkeypatch.setattr(repo, "facets", lambda **kwargs: {"all": 1, "success": 0, "pending": 0, "canceled": 0,
                                                          "failed": 0})
    users = MagicMock()
    users.by_ids.return_value = {USER_ID: {"id": USER_ID, "email": "a@x.com", "msisdn": "+9611"}}
    monkeypatch.setattr(routes, "service", OrderAdminService(repo, users))

    response = api_client.get(URL)

    assert response.status_code == 200
    assert "payment_intent_code" not in response.text
    assert "bundle_data" not in response.text
    assert "pi_secret" not in response.text
    row = response.json()["data"]["data"][0]
    assert row["bundle_name"] == "Europe 5GB"
    assert row["bundle_code"] == "EU-5GB"
    assert row["user"]["email"] == "a@x.com"


def test_teos117_filters_are_passed(api_client, as_admin, service):
    response = api_client.get(URL, params={"status": "failed", "payment_status": "success", "from": "2026-01-01",
                                           "to": "2026-01-31", "search": "EU-5GB", "sort": "order_status",
                                           "dir": "asc"})

    assert response.status_code == 200
    service.list.assert_called_once_with(PageParams(page=1, page_size=20), user_id=None,
                                         status=AdminOrderStatus.failed, payment_status=AdminPaymentStatus.success,
                                         date_from=date(2026, 1, 1), date_to=date(2026, 1, 31), search="EU-5GB",
                                         sort=AdminOrderSort.order_status, direction=AdminSortDirection.asc)


@pytest.mark.parametrize("status", ["all", "success", "pending", "canceled", "failed", "failure"])
def test_every_status_tab_is_accepted(api_client, as_admin, service, status):
    assert api_client.get(URL, params={"status": status}).status_code == 200
    assert service.list.call_args.kwargs["status"] == AdminOrderStatus(status)


def test_dir_wins_over_direction(api_client, as_admin, service):
    api_client.get(URL, params={"direction": "desc", "dir": "asc"})

    assert service.list.call_args.kwargs["direction"] == AdminSortDirection.asc


def test_facets_are_returned(api_client, as_admin, service):
    facets = {"all": 41, "success": 30, "pending": 5, "canceled": 4, "failed": 2}
    service.list.return_value = AdminPage(data=[ROW], total_count=41, facets=facets)

    body = api_client.get(URL).json()

    assert body["data"]["facets"] == facets


@pytest.mark.parametrize("params", [
    {"from": "2026-02-01", "to": "2026-01-31"},
    {"from": "2025-01-01", "to": "2026-01-02"},  # 367 days
])
def test_invalid_date_range_is_422_before_any_query(api_client, as_admin, monkeypatch, params):
    from app.api.v1.admin import orders as routes
    from app.services.admin.order_service import OrderAdminService

    orders, users = MagicMock(), MagicMock()
    monkeypatch.setattr(routes, "service", OrderAdminService(orders, users))

    response = api_client.get(URL, params=params)

    assert response.status_code == 422
    orders.search.assert_not_called()
    orders.facets.assert_not_called()
