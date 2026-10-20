"""HTTP contract of GET /api/v1/admin/orders (MS-5081)."""
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminUpstreamError
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.orders import AdminOrderSort, AdminOrderStatus, AdminSortDirection
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
                                         sort=AdminOrderSort.created_at, direction=AdminSortDirection.desc)


def test_filters_sort_and_paging_are_passed(api_client, as_admin, service):
    response = api_client.get(URL, params={"user_id": USER_ID, "status": "canceled", "sort": "amount",
                                           "direction": "asc", "page": 3, "page_size": 50})

    assert response.status_code == 200
    service.list.assert_called_once_with(PageParams(page=3, page_size=50), user_id=USER_ID,
                                         status=AdminOrderStatus.canceled, sort=AdminOrderSort.amount,
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
