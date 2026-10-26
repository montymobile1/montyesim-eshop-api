"""HTTP contract of /api/v1/admin/bundles (MS-5083). The lookup route is covered by test_lookups_api.py."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError, AdminValidationError
from app.schemas.admin.common import AdminPage
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/bundles"
BID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TAG1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TAG2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

ROW = {"id": BID, "bundle_name": "Europe 5GB", "is_active": True, "created_at": "2026-10-20T10:00:00",
       "updated_at": None, "data": {"bundle_info_code": "EU5", "bundle_name": "Europe 5 GB"}}
FACETS = {"all": 1, "active": 1, "inactive": 0}
TOTALS = {"total": 10, "active_count": 7, "inactive_count": 3}


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import bundles as routes
    fake = MagicMock(name="bundle-service")
    monkeypatch.setattr(routes, "service", fake)
    return fake


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_super_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_SUPER_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_reader():
    """An admin holding only admin.bundles.read."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="22222222-2222-2222-2222-222222222222", email="reader@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.bundles.read"}), token="t")
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def _failed(response, status):
    body = response.json()
    assert response.status_code == status
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    return body


def _code(response):
    return json.loads(response.json()["developerMessage"])["code"]


# ---------- list ----------

def test_list_returns_page_facets_and_totals(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[ROW], total_count=1, facets=FACETS, totals=TOTALS)

    response = api_client.get(BASE, params=[("search", "eu"), ("tag_ids", TAG1), ("tag_ids", TAG2),
                                            ("status", "active"), ("page", 2), ("page_size", 5)])

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 1
    assert body["data"]["facets"] == FACETS
    assert body["data"]["totals"] == TOTALS
    assert body["data"]["data"][0]["data"]["bundle_info_code"] == "EU5"
    search, tag_ids, status, page = service.list.call_args.args
    assert (search, tag_ids, status, page.page, page.page_size) == ("eu", [TAG1, TAG2], "active", 2, 5)


def test_list_defaults_to_all_page_one(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[], total_count=0, facets=FACETS, totals=TOTALS)

    assert api_client.get(BASE).status_code == 200
    search, tag_ids, status, page = service.list.call_args.args
    assert (search, tag_ids, status, page.page, page.page_size) == (None, None, "all", 1, 20)


@pytest.mark.parametrize("params", [
    {"page_size": 101}, {"status": "foo"}, {"foo": "bar"}, {"page": 0}, {"tag_ids": "not-a-uuid"},
    {"tag_ids[]": TAG1},
])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)
    service.list.assert_not_called()


def test_list_search_is_passed_literally(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[], total_count=0, facets=FACETS, totals=TOTALS)

    api_client.get(BASE, params={"search": "a,is_active.eq.false"})

    assert service.list.call_args.args[0] == "a,is_active.eq.false"


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)


def test_list_database_error_is_502(api_client, service, as_admin):
    service.list.side_effect = AdminUpstreamError("DATABASE_ERROR")
    _failed(api_client.get(BASE), 502)


def test_lookup_route_still_answers_before_the_id_route(api_client, as_admin, monkeypatch):
    from app.services.admin import lookup_service
    fake = MagicMock()
    fake.bundles.return_value = AdminPage(data=[{"id": BID, "label": "Europe (EU5)", "bundle_code": "EU5"}],
                                          total_count=1)
    monkeypatch.setattr(lookup_service, "service", fake)

    response = api_client.get(f"{BASE}/lookup", params={"search": "EU"})

    assert response.status_code == 200
    assert response.json()["data"]["data"][0]["bundle_code"] == "EU5"


# ---------- detail ----------

def test_get_returns_bundle_with_tags(api_client, service, as_admin):
    service.get.return_value = {**ROW, "tags": [{"bundle_tag_id": 3, "tag": {
        "id": TAG1, "name": "France", "icon": None,
        "group": {"id": 2, "name": "Europe", "type": 1, "group_category": "country"}}}]}

    response = api_client.get(f"{BASE}/{BID}")

    assert response.status_code == 200
    tag = response.json()["data"]["tags"][0]
    assert tag["bundle_tag_id"] == 3 and tag["tag"]["group"]["name"] == "Europe"
    assert service.get.call_args.args[0] == BID


def test_get_unknown_id_is_404(api_client, service, as_admin):
    service.get.side_effect = AdminNotFound("not_found")
    _failed(api_client.get(f"{BASE}/{TAG1}"), 404)


def test_get_not_a_uuid_is_422(api_client, service, as_admin):
    _failed(api_client.get(f"{BASE}/123"), 422)
    service.get.assert_not_called()


# ---------- rename ----------

def test_rename_patches_name_with_audit_context(api_client, service, as_admin):
    service.rename.return_value = {"id": BID, "bundle_name": "New name"}

    response = api_client.patch(f"{BASE}/{BID}", json={"bundle_name": "  New name  "},
                                headers={"X-Forwarded-For": "9.9.9.9"})

    assert response.status_code == 200
    assert response.json()["data"] == {"id": BID, "bundle_name": "New name"}
    bundle_id, body, ctx = service.rename.call_args.args
    assert bundle_id == BID and body.bundle_name == "New name"
    assert ctx.actor_id == "11111111-1111-1111-1111-111111111111" and ctx.ip == "9.9.9.9"


@pytest.mark.parametrize("payload", [
    {"bundle_name": ""}, {"bundle_name": "   "}, {"bundle_name": "x" * 61}, {}, {"bundle_name": "ok", "is_active": 1},
])
def test_rename_invalid_body_is_422(api_client, service, as_admin, payload):
    _failed(api_client.patch(f"{BASE}/{BID}", json=payload), 422)
    service.rename.assert_not_called()


def test_rename_accepts_sixty_characters(api_client, service, as_admin):
    service.rename.return_value = {"id": BID, "bundle_name": "x" * 60}
    assert api_client.patch(f"{BASE}/{BID}", json={"bundle_name": "x" * 60}).status_code == 200


def test_rename_unknown_id_is_404(api_client, service, as_admin):
    service.rename.side_effect = AdminNotFound("not_found")
    _failed(api_client.patch(f"{BASE}/{BID}", json={"bundle_name": "n"}), 404)


def test_rename_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.patch(f"{BASE}/{BID}", json={"bundle_name": "n"}), 403)
    service.rename.assert_not_called()


# ---------- status ----------

def test_status_deactivate_passes_reason(api_client, service, as_super_admin):
    service.set_status.return_value = {"id": BID, "is_active": False}

    response = api_client.patch(f"{BASE}/{BID}/status", json={"is_active": False, "reason": "maintenance"})

    assert response.status_code == 200
    assert response.json()["data"]["is_active"] is False
    bundle_id, body, ctx = service.set_status.call_args.args
    assert bundle_id == BID and body.is_active is False and ctx.reason == "maintenance"


def test_status_activate_needs_no_reason(api_client, service, as_super_admin):
    service.set_status.return_value = {"id": BID, "is_active": True}

    assert api_client.patch(f"{BASE}/{BID}/status", json={"is_active": True}).status_code == 200
    assert service.set_status.call_args.args[2].reason is None


@pytest.mark.parametrize("payload", [
    {"is_active": False}, {"is_active": False, "reason": "   "}, {}, {"is_active": True, "extra": 1},
    {"is_active": True, "reason": "x" * 501},
])
def test_status_bad_body_is_422(api_client, service, as_super_admin, payload):
    _failed(api_client.patch(f"{BASE}/{BID}/status", json=payload), 422)
    service.set_status.assert_not_called()


def test_status_same_state_is_409_no_change(api_client, service, as_super_admin):
    service.set_status.side_effect = AdminConflict("no_change")

    response = api_client.patch(f"{BASE}/{BID}/status", json={"is_active": True})

    _failed(response, 409)
    assert _code(response) == "no_change"


def test_status_unknown_id_is_404(api_client, service, as_super_admin):
    service.set_status.side_effect = AdminNotFound("not_found")
    _failed(api_client.patch(f"{BASE}/{BID}/status", json={"is_active": True}), 404)


def test_status_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.patch(f"{BASE}/{BID}/status", json={"is_active": True}), 403)


def test_status_is_super_admin_only_as_today(api_client, service, as_admin):
    _failed(api_client.patch(f"{BASE}/{BID}/status", json={"is_active": True}), 403)
    service.set_status.assert_not_called()


# ---------- bulk ----------

def test_bulk_deactivate_is_super_admin_only(api_client, service, as_admin):
    _failed(api_client.post(f"{BASE}/status/bulk", json={"is_active": False, "reason": "provider down"}), 403)
    service.set_all_status.assert_not_called()


def test_bulk_deactivate_returns_affected_ids(api_client, service, as_super_admin):
    service.set_all_status.return_value = {"rows_affected": 2, "affected_bundle_ids": [BID, TAG1]}

    response = api_client.post(f"{BASE}/status/bulk", json={"is_active": False, "reason": "provider down"})

    assert response.status_code == 200
    assert response.json()["data"] == {"rows_affected": 2, "affected_bundle_ids": [BID, TAG1]}
    body, ctx = service.set_all_status.call_args.args
    assert body.is_active is False and ctx.reason == "provider down"


def test_bulk_activate_without_reason_is_allowed(api_client, service, as_super_admin):
    service.set_all_status.return_value = {"rows_affected": 1, "affected_bundle_ids": [BID]}
    assert api_client.post(f"{BASE}/status/bulk", json={"is_active": True}).status_code == 200


def test_bulk_deactivate_without_reason_is_422(api_client, service, as_super_admin):
    _failed(api_client.post(f"{BASE}/status/bulk", json={"is_active": False}), 422)
    service.set_all_status.assert_not_called()


def test_bulk_no_change_is_409(api_client, service, as_super_admin):
    service.set_all_status.side_effect = AdminConflict("no_change")

    response = api_client.post(f"{BASE}/status/bulk", json={"is_active": True})

    _failed(response, 409)
    assert _code(response) == "no_change"


def test_bulk_without_session_is_401(api_client, service):
    _failed(api_client.post(f"{BASE}/status/bulk", json={"is_active": True}), 401)


# ---------- tags ----------

def test_set_tags_replaces_the_set(api_client, service, as_admin):
    service.set_tags.return_value = {"id": BID, "tag_ids": [TAG1, TAG2]}

    response = api_client.put(f"{BASE}/{BID}/tags", json={"tag_ids": [TAG1, TAG2]})

    assert response.status_code == 200
    assert response.json()["data"]["tag_ids"] == [TAG1, TAG2]
    bundle_id, body, ctx = service.set_tags.call_args.args
    assert bundle_id == BID and [str(t) for t in body.tag_ids] == [TAG1, TAG2]
    assert ctx.actor_id == "11111111-1111-1111-1111-111111111111"


def test_set_tags_accepts_an_empty_set(api_client, service, as_admin):
    """MS-5065: clearing every tag must work."""
    service.set_tags.return_value = {"id": BID, "tag_ids": []}

    response = api_client.put(f"{BASE}/{BID}/tags", json={"tag_ids": []})

    assert response.status_code == 200
    assert response.json()["data"]["tag_ids"] == []
    assert service.set_tags.call_args.args[1].tag_ids == []


def test_set_tags_unknown_tag_is_422(api_client, service, as_admin):
    service.set_tags.side_effect = AdminValidationError("unknown tag id(s): x")
    _failed(api_client.put(f"{BASE}/{BID}/tags", json={"tag_ids": [TAG1]}), 422)


@pytest.mark.parametrize("payload", [{}, {"tag_ids": ["nope"]}, {"tag_ids": None}, {"tag_ids": [], "x": 1}])
def test_set_tags_bad_body_is_422(api_client, service, as_admin, payload):
    _failed(api_client.put(f"{BASE}/{BID}/tags", json=payload), 422)
    service.set_tags.assert_not_called()


def test_set_tags_unknown_bundle_is_404(api_client, service, as_admin):
    service.set_tags.side_effect = AdminNotFound("not_found")
    _failed(api_client.put(f"{BASE}/{BID}/tags", json={"tag_ids": []}), 404)


def test_set_tags_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.put(f"{BASE}/{BID}/tags", json={"tag_ids": []}), 403)
    service.set_tags.assert_not_called()
