"""HTTP contract of GET /api/v1/admin/devices (MS-5081)."""
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminUpstreamError
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.devices import DeviceLoggedIn, DevicePlatform, DeviceSort
from app.schemas.admin.orders import AdminSortDirection
from tests.admin.conftest import make_admin

URL = "/api/v1/admin/devices"
USER_ID = "22222222-2222-2222-2222-222222222222"
ROW = {"id": 5, "user_id": USER_ID, "manufacturer": "Apple", "device_model": "iPhone"}


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import devices as routes
    fake = MagicMock()
    fake.list.return_value = AdminPage(data=[ROW], total_count=1)
    monkeypatch.setattr(routes, "service", fake)
    return fake


def test_list(api_client, as_admin, service):
    response = api_client.get(URL)

    assert response.status_code == 200
    assert response.json()["data"]["data"] == [ROW]
    assert response.json()["totalCount"] == 1
    service.list.assert_called_once_with(
        PageParams(page=1, page_size=20), user_id=None, platform=DevicePlatform.all,
        logged_in=DeviceLoggedIn.all, sort=DeviceSort.created_at, direction=AdminSortDirection.desc)


def test_user_filter_and_paging(api_client, as_admin, service):
    response = api_client.get(URL, params={"user_id": USER_ID, "page": 2, "page_size": 10, "platform": "ios",
                                           "logged_in": "yes", "sort": "timestamp_login", "dir": "asc"})

    assert response.status_code == 200
    service.list.assert_called_once_with(
        PageParams(page=2, page_size=10), user_id=USER_ID, platform=DevicePlatform.ios,
        logged_in=DeviceLoggedIn.yes, sort=DeviceSort.timestamp_login, direction=AdminSortDirection.asc)


@pytest.mark.parametrize("params", [{"user_id": "x"}, {"page_size": 101}, {"user": USER_ID}])
def test_invalid_query_is_422(api_client, as_admin, service, params):
    assert api_client.get(URL, params=params).status_code == 422
    service.list.assert_not_called()


def test_no_session_is_401(api_client, service):
    assert api_client.get(URL).status_code == 401


def test_missing_permission_is_403(api_client, service):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="u", email="x@example.com", role=ROLE_ADMIN, permissions=frozenset({"admin.orders.read"}))
    try:
        response = api_client.get(URL)
    finally:
        esim_app.dependency_overrides.pop(admin_user, None)

    assert response.status_code == 403
    assert response.json()["developerMessage"] == "admin.devices.read"


def test_database_error_is_502(api_client, as_admin, service):
    service.list.side_effect = AdminUpstreamError("DATABASE_ERROR")

    assert api_client.get(URL).status_code == 502
