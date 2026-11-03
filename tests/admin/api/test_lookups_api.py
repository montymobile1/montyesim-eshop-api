"""HTTP contract of the MS-5077 lookups: GET /api/v1/admin/<resource>/lookup."""
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminUpstreamError
from app.schemas.admin.common import AdminPage, LookupItem
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin"

# path -> (LookupService method, permission, one item as the service returns it)
LOOKUPS = {
    "users/lookup": ("users", "admin.users.read", {"id": "u1", "label": "a@x.com", "email": "a@x.com"}),
    "bundles/lookup": ("bundles", "admin.bundles.read",
                       {"id": "b1", "label": "Europe 5GB (EU5)", "bundle_code": "EU5"}),
    "groups/lookup": ("groups", "admin.groups.read",
                      {"id": "7", "label": "Regions", "type": 1, "group_category": "region"}),
    "tags/lookup": ("tags", "admin.tags.read", {"id": "t1", "label": "France (Countries)"}),
    "promotion-rules/lookup": ("promotion_rules", "admin.rules.read", {"id": "r1", "label": "Cashback/Buy/1"}),
}


@pytest.fixture
def lookup_service(monkeypatch):
    fake = MagicMock(name="lookup-service")
    for method, _, item in LOOKUPS.values():
        getattr(fake, method).return_value = AdminPage[LookupItem](data=[LookupItem(**item)], total_count=42)
    monkeypatch.setattr("app.services.admin.lookup_service.service", fake)
    return fake


@pytest.fixture
def as_admin(api_client):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_user_without(api_client):
    """Override that returns an admin lacking one permission; returns the setter."""
    from app.main import esim_app

    def set_missing(permission):
        user = make_admin(ROLE_ADMIN)
        esim_app.dependency_overrides[admin_user] = lambda: replace(user, permissions=user.permissions - {permission})

    yield set_missing
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.mark.parametrize("path", LOOKUPS)
def test_lookup_returns_items_and_exact_total(api_client, as_admin, lookup_service, path):
    method, _, item = LOOKUPS[path]

    response = api_client.get(f"{BASE}/{path}", params={"search": "ab", "page": 2, "page_size": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["data"] == [item]
    assert body["data"]["total_count"] == 42
    assert body["totalCount"] == 42
    search, page, record_id = getattr(lookup_service, method).call_args.args
    assert search == "ab"
    assert (page.page, page.page_size) == (2, 10)


@pytest.mark.parametrize("path", LOOKUPS)
def test_lookup_defaults_to_first_page_without_search(api_client, as_admin, lookup_service, path):
    method = LOOKUPS[path][0]

    assert api_client.get(f"{BASE}/{path}").status_code == 200

    search, page, record_id = getattr(lookup_service, method).call_args.args
    assert search is None
    assert record_id is None
    assert (page.page, page.page_size) == (1, 20)


@pytest.mark.parametrize("path", LOOKUPS)
def test_lookup_by_id_passes_the_id_for_restoring_a_label(api_client, as_admin, lookup_service, path):
    method = LOOKUPS[path][0]

    assert api_client.get(f"{BASE}/{path}", params={"id": "u1"}).status_code == 200

    search, page, record_id = getattr(lookup_service, method).call_args.args
    assert (search, record_id) == (None, "u1")


def test_lookup_rejects_an_empty_id(api_client, as_admin, lookup_service):
    assert api_client.get(f"{BASE}/users/lookup", params={"id": ""}).status_code == 422


@pytest.mark.parametrize("path", LOOKUPS)
def test_lookup_without_session_is_401(api_client, lookup_service, path):
    response = api_client.get(f"{BASE}/{path}")

    assert response.status_code == 401
    assert response.json()["responseCode"] == 401


@pytest.mark.parametrize("path", LOOKUPS)
def test_lookup_without_the_resource_read_permission_is_403(api_client, as_user_without, lookup_service, path):
    method, permission, _ = LOOKUPS[path]
    as_user_without(permission)

    response = api_client.get(f"{BASE}/{path}")

    assert response.status_code == 403
    assert response.json()["developerMessage"] == permission
    getattr(lookup_service, method).assert_not_called()


@pytest.mark.parametrize("path", LOOKUPS)
@pytest.mark.parametrize("params", [{"page_size": 101}, {"page": 0}, {"page_size": 0}, {"status": "active"},
                                    {"search": "x" * 101}])
def test_lookup_rejects_bad_or_unknown_params_with_422(api_client, as_admin, lookup_service, path, params):
    response = api_client.get(f"{BASE}/{path}", params=params)

    assert response.status_code == 422
    assert response.json()["responseCode"] == 422


def test_lookup_database_error_is_502(api_client, as_admin, lookup_service):
    lookup_service.tags.side_effect = AdminUpstreamError("DATABASE_ERROR")

    response = api_client.get(f"{BASE}/tags/lookup", params={"search": "%"})

    assert response.status_code == 502
    assert response.json()["developerMessage"] == "DATABASE_ERROR"


def test_lookup_routes_are_in_openapi(api_client):
    paths = api_client.get("/openapi.json").json()["paths"]

    for path in LOOKUPS:
        assert f"{BASE}/{path}" in paths
