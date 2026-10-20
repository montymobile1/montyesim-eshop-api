"""HTTP contract of /api/v1/admin/banners (MS-5058, MS-5060)."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminPayloadTooLarge
from app.schemas.admin.common import AdminPage
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/banners"

ROW = {"id": 3, "title": "Refer", "description": "Invite friends",
       "image_url": "https://x.supabase.co/storage/v1/object/public/media/banners-web/a.png",
       "action": "REFER_NOW", "platform": "web", "is_active": True, "created_at": "2026-10-20T10:00:00"}
FORM = {"title": " Refer ", "description": "Invite friends", "platform": "web", "action": "REFER_NOW"}
PNG = ("banner.png", b"\x89PNG\r\n\x1a\nfake", "image/png")


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import banners as routes
    fake = MagicMock(name="banner-service")
    fake.read_image.side_effect = lambda file: file.read()
    monkeypatch.setattr(routes, "service", fake)
    return fake


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_reader():
    """An admin holding only admin.banners.read."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="22222222-2222-2222-2222-222222222222", email="reader@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.banners.read"}), token="t")
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def _failed(response, status):
    body = response.json()
    assert response.status_code == status, body
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    return body


def _code(response):
    return json.loads(response.json()["developerMessage"])["code"]


# ---------- list / get ----------

def test_list_returns_rows_and_facets(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[ROW], total_count=1, facets={"all": 1, "active": 1, "inactive": 0})

    response = api_client.get(BASE, params={"search": "re", "status": "active", "page_size": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["data"][0]["image_url"].endswith("banners-web/a.png")
    assert body["data"]["facets"] == {"all": 1, "active": 1, "inactive": 0}
    search, status, page = service.list.call_args.args
    assert (search, status, page.page_size) == ("re", "active", 10)


@pytest.mark.parametrize("params", [{"page_size": 101}, {"status": "foo"}, {"name": "x"}])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)


def test_list_without_read_permission_is_403(api_client, service):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="3", email="x@example.com", role=ROLE_ADMIN, permissions=frozenset(), token="t")
    try:
        _failed(api_client.get(BASE), 403)
    finally:
        esim_app.dependency_overrides.pop(admin_user, None)


def test_get_returns_banner(api_client, service, as_admin):
    service.get.return_value = ROW
    response = api_client.get(f"{BASE}/3")
    assert response.status_code == 200
    assert response.json()["data"]["title"] == "Refer"
    service.get.assert_called_once_with(3)


def test_get_unknown_is_404_and_malformed_is_422(api_client, service, as_admin):
    service.get.side_effect = AdminNotFound("not_found")
    _failed(api_client.get(f"{BASE}/999"), 404)
    _failed(api_client.get(f"{BASE}/abc"), 422)


# ---------- create ----------

def test_create_multipart_returns_201(api_client, service, as_admin):
    service.create.return_value = ROW

    response = api_client.post(BASE, data=FORM, files={"image": PNG}, headers={"X-Forwarded-For": "8.8.8.8"})

    assert response.status_code == 201
    assert response.json()["responseCode"] == 201
    fields, data, ctx = service.create.call_args.args
    assert fields == {"title": "Refer", "description": "Invite friends", "platform": "web", "action": "REFER_NOW"}
    assert data == PNG[1]
    assert ctx.ip == "8.8.8.8"


def test_create_passes_is_active_and_empty_action_as_null(api_client, service, as_admin):
    service.create.return_value = ROW

    response = api_client.post(BASE, data={**FORM, "action": "", "is_active": "false"}, files={"image": PNG})

    assert response.status_code == 201
    fields = service.create.call_args.args[0]
    assert fields["is_active"] is False and fields["action"] is None


def test_create_without_image_is_422(api_client, service, as_admin):
    _failed(api_client.post(BASE, data=FORM), 422)
    service.create.assert_not_called()


@pytest.mark.parametrize("override", [
    {"title": ""}, {"title": "   "}, {"title": "x" * 101}, {"description": "y" * 501},
    {"platform": "tv"}, {"action": "javascript:alert(1)"}, {"link": "https://example.com"}, {"is_active": "yes"},
])
def test_create_invalid_fields_are_422(api_client, service, as_admin, override):
    _failed(api_client.post(BASE, data={**FORM, **override}, files={"image": PNG}), 422)
    service.create.assert_not_called()


def test_create_duplicate_title_is_409(api_client, service, as_admin):
    service.create.side_effect = AdminConflict("duplicate_title")
    response = api_client.post(BASE, data=FORM, files={"image": PNG})
    _failed(response, 409)
    assert _code(response) == "duplicate_title"


def test_create_too_large_is_413(api_client, service, as_admin):
    service.create.side_effect = AdminPayloadTooLarge("file larger than 2097152 bytes")
    _failed(api_client.post(BASE, data=FORM, files={"image": PNG}), 413)


def test_create_read_only_admin_is_403_and_nothing_uploaded(api_client, service, as_reader):
    _failed(api_client.post(BASE, data=FORM, files={"image": PNG}), 403)
    service.create.assert_not_called()
    service.read_image.assert_not_called()


def test_create_without_session_is_401(api_client, service):
    _failed(api_client.post(BASE, data=FORM, files={"image": PNG}), 401)


# ---------- update ----------

def test_update_without_image_keeps_it(api_client, service, as_admin):
    service.update.return_value = ROW

    response = api_client.put(f"{BASE}/3", data=FORM)

    assert response.status_code == 200
    banner_id, fields, data, _ctx = service.update.call_args.args
    assert banner_id == 3 and data is None and fields["title"] == "Refer"


def test_update_with_image_sends_bytes(api_client, service, as_admin):
    service.update.return_value = ROW
    response = api_client.put(f"{BASE}/3", data=FORM, files={"image": PNG})
    assert response.status_code == 200
    assert service.update.call_args.args[2] == PNG[1]


def test_update_unknown_is_404(api_client, service, as_admin):
    service.update.side_effect = AdminNotFound("not_found")
    _failed(api_client.put(f"{BASE}/999", data=FORM), 404)


def test_update_unknown_field_is_422(api_client, service, as_admin):
    _failed(api_client.put(f"{BASE}/3", data={**FORM, "sort_order": "1"}), 422)
    service.update.assert_not_called()


# ---------- delete ----------

def test_delete_is_204(api_client, service, as_admin):
    response = api_client.delete(f"{BASE}/3")
    assert response.status_code == 204
    assert response.content == b""
    assert service.delete.call_args.args[0] == 3


def test_delete_twice_second_is_404(api_client, service, as_admin):
    service.delete.side_effect = [None, AdminNotFound("not_found")]
    assert api_client.delete(f"{BASE}/3").status_code == 204
    _failed(api_client.delete(f"{BASE}/3"), 404)


def test_delete_read_only_admin_is_403(api_client, service, as_reader):
    _failed(api_client.delete(f"{BASE}/3"), 403)
    service.delete.assert_not_called()
