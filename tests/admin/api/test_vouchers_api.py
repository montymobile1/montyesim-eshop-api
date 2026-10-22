"""HTTP contract of /api/v1/admin/vouchers (MS-5089)."""
import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminConflict, AdminNotFound
from app.schemas.admin.common import AdminPage
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/vouchers"

ROW = {"id": 7, "code": "SUMMER", "amount": 10.5, "is_used": False, "is_active": True,
       "expired_at": None, "created_at": "2026-10-20T10:00:00", "used_by_user": None}


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import vouchers as routes
    fake = MagicMock(name="voucher-service")
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
    """An admin holding only admin.vouchers.read."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="22222222-2222-2222-2222-222222222222", email="reader@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.vouchers.read"}), token="t")
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

def test_list_returns_page_with_facets_and_used_by_user(api_client, service, as_admin):
    used = {**ROW, "id": 8, "is_used": True, "used_by_user": {"id": "u1", "email": "u1@example.com"}}
    service.list.return_value = AdminPage(data=[ROW, used], total_count=2,
                                          facets={"all": 2, "active": 2, "inactive": 0})

    response = api_client.get(BASE, params={"search": "50%", "status": "active", "page": 2, "page_size": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 2
    assert body["data"]["total_count"] == 2
    assert body["data"]["facets"] == {"all": 2, "active": 2, "inactive": 0}
    assert body["data"]["data"][1]["used_by_user"] == {"id": "u1", "email": "u1@example.com"}
    search, status, page = service.list.call_args.args
    assert (search, status, page.page, page.page_size) == ("50%", "active", 2, 5)


def test_list_defaults_to_all_page_one(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[], total_count=0, facets={"all": 0, "active": 0, "inactive": 0})

    assert api_client.get(BASE).status_code == 200
    search, status, page = service.list.call_args.args
    assert (search, status, page.page, page.page_size) == (None, "all", 1, 20)


@pytest.mark.parametrize("params", [{"page_size": 101}, {"status": "bogus"}, {"foo": "bar"}, {"page": 0}])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)
    service.list.assert_not_called()


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)


# ---------- create ----------

def test_create_returns_201(api_client, service, as_admin):
    service.create.return_value = ROW
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    response = api_client.post(BASE, json={"code": "SUMMER", "amount": 10.5, "expired_at": tomorrow},
                               headers={"X-Forwarded-For": "9.9.9.9"})

    assert response.status_code == 201
    assert response.json()["responseCode"] == 201
    assert response.json()["data"]["code"] == "SUMMER"
    body, ctx = service.create.call_args.args
    assert body.code == "SUMMER" and body.expired_at.isoformat() == tomorrow
    assert ctx.actor_id == "11111111-1111-1111-1111-111111111111"
    assert ctx.ip == "9.9.9.9"


def test_create_duplicate_code_is_409(api_client, service, as_admin):
    service.create.side_effect = AdminConflict("duplicate_code")

    response = api_client.post(BASE, json={"code": "summer", "amount": 1})

    _failed(response, 409)
    assert _code(response) == "duplicate_code"


@pytest.mark.parametrize("payload", [
    {"code": "SUMMER", "amount": 0},
    {"code": "SUMMER", "amount": -1},
    {"code": "has space", "amount": 1},
    {"code": "X", "amount": 1},
    {"code": "ABC", "amount": 1},
    {"code": "A" * 33, "amount": 1},
    {"code": "SUM_MER", "amount": 1},
    {"code": "SUMMER", "amount": "abc"},
    {"code": "SUMMER", "amount": 1, "expired_at": "2000-01-01"},
    {"code": "SUMMER", "amount": 1, "expired_at": "not-a-date"},
    {"code": "SUMMER", "amount": 1, "is_used": True},
    {"code": "SUMMER", "amount": 1, "used_by": "x"},
    {},
])
def test_create_invalid_body_is_422(api_client, service, as_admin, payload):
    _failed(api_client.post(BASE, json=payload), 422)
    service.create.assert_not_called()


def test_create_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.post(BASE, json={"code": "SUMMER", "amount": 1}), 403)
    service.create.assert_not_called()


# ---------- status ----------

def test_status_patch_passes_reason_to_audit(api_client, service, as_admin):
    service.set_status.return_value = {**ROW, "is_active": False}

    response = api_client.patch(f"{BASE}/7/status", json={"is_active": False, "reason": "expired campaign"})

    assert response.status_code == 200
    assert response.json()["data"]["is_active"] is False
    voucher_id, body, ctx = service.set_status.call_args.args
    assert voucher_id == 7 and body.is_active is False and ctx.reason == "expired campaign"


def test_status_same_state_is_409_no_change(api_client, service, as_admin):
    service.set_status.side_effect = AdminConflict("no_change")

    response = api_client.patch(f"{BASE}/7/status", json={"is_active": True})

    _failed(response, 409)
    assert _code(response) == "no_change"


def test_status_unknown_id_is_404(api_client, service, as_admin):
    service.set_status.side_effect = AdminNotFound("not_found")
    _failed(api_client.patch(f"{BASE}/999/status", json={"is_active": True}), 404)


@pytest.mark.parametrize("path, payload", [
    ("abc/status", {"is_active": True}),
    ("7/status", {}),
    ("7/status", {"is_active": True, "extra": 1}),
])
def test_status_bad_request_is_422(api_client, service, as_admin, path, payload):
    _failed(api_client.patch(f"{BASE}/{path}", json=payload), 422)


# ---------- delete ----------

def test_delete_returns_204_empty(api_client, service, as_admin):
    response = api_client.delete(f"{BASE}/7")

    assert response.status_code == 204
    assert response.content == b""
    assert service.delete.call_args.args[0] == 7


def test_delete_used_voucher_is_409_voucher_used(api_client, service, as_admin):
    service.delete.side_effect = AdminConflict("voucher_used")

    response = api_client.delete(f"{BASE}/7")

    _failed(response, 409)
    assert _code(response) == "voucher_used"


def test_delete_twice_second_is_404(api_client, service, as_admin):
    service.delete.side_effect = [None, AdminNotFound("not_found")]

    assert api_client.delete(f"{BASE}/7").status_code == 204
    _failed(api_client.delete(f"{BASE}/7"), 404)


def test_delete_without_session_is_401(api_client, service):
    _failed(api_client.delete(f"{BASE}/7"), 401)
    service.delete.assert_not_called()


# ---------- TEOS-133 ----------

@pytest.mark.parametrize("status", ["all", "active", "inactive", "used", "expired"])
def test_list_accepts_every_status_tab(api_client, service, as_admin, status):
    service.list.return_value = AdminPage(data=[], total_count=0,
                                          facets={"all": 0, "active": 0, "inactive": 0, "used": 0, "expired": 0})

    response = api_client.get(BASE, params={"status": status})

    assert response.status_code == 200
    assert service.list.call_args.args[1] == status
    assert set(response.json()["data"]["facets"]) == {"all", "active", "inactive", "used", "expired"}


def test_create_without_code_is_accepted(api_client, service, as_admin):
    service.create.return_value = {**ROW, "code": "ABCDEFGHJKMN"}

    response = api_client.post(BASE, json={"amount": 3})

    assert response.status_code == 201
    assert response.json()["data"]["code"] == "ABCDEFGHJKMN"
    assert service.create.call_args.args[0].code is None


def test_create_explicit_code_is_upper_cased(api_client, service, as_admin):
    service.create.return_value = ROW

    api_client.post(BASE, json={"code": "summer-25", "amount": 3})

    assert service.create.call_args.args[0].code == "SUMMER-25"


def test_activate_used_voucher_is_409_already_used(api_client, service, as_admin):
    service.set_status.side_effect = AdminConflict("already_used")

    response = api_client.patch(f"{BASE}/7/status", json={"is_active": True})

    _failed(response, 409)
    assert _code(response) == "already_used"


def test_writes_without_write_permission_are_403(api_client, service, as_reader):
    _failed(api_client.patch(f"{BASE}/7/status", json={"is_active": True}), 403)
    _failed(api_client.delete(f"{BASE}/7"), 403)
    service.set_status.assert_not_called()
    service.delete.assert_not_called()


def test_list_and_create_with_the_real_service(api_client, as_admin, monkeypatch):
    """The real service with its repositories mocked: amount cap from the setting, used_by and facets."""
    from app.api.v1.admin import vouchers as routes
    from app.repo.admin.base import Paged
    from app.services.admin.voucher_service import VoucherAdminService
    repo, users, settings = MagicMock(), MagicMock(), MagicMock()
    settings.value_of.side_effect = lambda key: {"VOUCHER_MAX_AMOUNT": "100", "DEFAULT_CURRENCY": "EUR"}.get(key)
    monkeypatch.setattr(routes, "service", VoucherAdminService(repo=repo, users=users, settings=settings))
    repo.search.return_value = Paged(rows=[{"id": 1, "code": "USED1", "amount": 5, "is_used": True,
                                            "used_by": "u1", "is_active": False}], total=1)
    repo.facets.return_value = {"all": 1, "active": 0, "inactive": 1, "used": 1, "expired": 0}
    users.by_ids.return_value = {"u1": {"id": "u1", "email": "u1@example.com"}}

    listed = api_client.get(BASE, params={"status": "used"}).json()["data"]
    too_much = api_client.post(BASE, json={"code": "BIGONE", "amount": 101})

    assert listed["data"][0]["used_by"] == {"id": "u1", "email": "u1@example.com"}
    assert listed["data"][0]["currency"] == "EUR"
    assert listed["facets"]["used"] == 1
    _failed(too_much, 422)
    assert "VOUCHER_MAX_AMOUNT" in too_much.json()["developerMessage"]
    repo.insert_audited.assert_not_called()
