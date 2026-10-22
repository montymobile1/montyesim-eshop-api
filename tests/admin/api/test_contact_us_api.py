"""HTTP contract of GET /api/v1/admin/contact-us (MS-5081, TEOS-118)."""
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminUpstreamError
from app.schemas.admin.common import AdminPage, PageParams
from tests.admin.conftest import make_admin

URL = "/api/v1/admin/contact-us"
ROW = {"id": 3, "email": "a@x.com", "content": "hello", "created_at": "2026-10-01T10:00:00"}


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import contact_us as routes
    fake = MagicMock()
    fake.list.return_value = AdminPage(data=[ROW], total_count=1)
    monkeypatch.setattr(routes, "service", fake)
    return fake


def test_list(api_client, as_admin, service):
    response = api_client.get(URL)

    assert response.status_code == 200
    assert response.json()["data"]["data"] == [ROW]
    service.list.assert_called_once_with(PageParams(page=1, page_size=20), search=None, date_from=None, date_to=None)


def test_search_and_paging(api_client, as_admin, service):
    response = api_client.get(URL, params={"search": "50%_a", "page": 2, "page_size": 100})

    assert response.status_code == 200
    service.list.assert_called_once_with(PageParams(page=2, page_size=100), search="50%_a",
                                         date_from=None, date_to=None)


@pytest.mark.parametrize("params", [{"page_size": 101}, {"page": 0}, {"name": "a"}, {"search": "x" * 101},
                                    {"from": "2026-13-01"}, {"to": "yesterday"}])
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
    assert response.json()["developerMessage"] == "admin.contact_us.read"


def test_database_error_is_502(api_client, as_admin, service):
    service.list.side_effect = AdminUpstreamError("DATABASE_ERROR")

    assert api_client.get(URL).status_code == 502


def test_search_of_100_characters_is_accepted(api_client, as_admin, service):
    assert api_client.get(URL, params={"search": "x" * 100}).status_code == 200


def test_date_range_is_passed_as_dates(api_client, as_admin, service):
    from datetime import date

    response = api_client.get(URL, params={"from": "2026-10-01", "to": "2026-10-21", "search": "مرحبا"})

    assert response.status_code == 200
    service.list.assert_called_once_with(PageParams(page=1, page_size=20), search="مرحبا",
                                         date_from=date(2026, 10, 1), date_to=date(2026, 10, 21))


def test_from_after_to_is_422(api_client, as_admin):
    # the real service rejects the range before any query
    from app.api.v1.admin import contact_us as routes
    from app.services.admin.contact_us_service import ContactUsAdminService
    repo = MagicMock()
    original = routes.service
    routes.service = ContactUsAdminService(repo)
    try:
        response = api_client.get(URL, params={"from": "2026-10-21", "to": "2026-10-01"})
    finally:
        routes.service = original

    assert response.status_code == 422
    repo.search.assert_not_called()


def test_rows_are_returned_as_stored(api_client, as_admin, service):
    row = {"id": 4, "email": "Mixed.Case@X.com", "content": "<script>alert(1)</script>\nline 2 " + "ل" * 5000,
           "created_at": "2026-10-02T09:00:00"}
    service.list.return_value = AdminPage(data=[row], total_count=1)

    body = api_client.get(URL).json()["data"]

    assert body["data"] == [row]
    assert body["total_count"] == 1


def test_empty_and_past_the_end_pages(api_client, as_admin, service):
    service.list.return_value = AdminPage(data=[], total_count=3)

    body = api_client.get(URL, params={"page": 9}).json()["data"]

    assert body["data"] == [] and body["total_count"] == 3


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_write_routes(api_client, as_admin, service, method):
    assert getattr(api_client, method)(URL).status_code == 405
    assert getattr(api_client, method)(URL + "/1").status_code in (404, 405)


def test_openapi_documents_filters():
    from app.main import esim_app
    operation = esim_app.openapi()["paths"][URL]["get"]
    names = {p["name"] for p in operation["parameters"]}

    assert {"search", "from", "to", "page", "page_size"} <= names
    assert "post" not in esim_app.openapi()["paths"][URL]
