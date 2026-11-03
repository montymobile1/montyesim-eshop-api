"""HTTP contract of /api/v1/admin/users (MS-5079)."""
import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError
from app.schemas.admin.common import AdminPage, PageParams
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/users"
USER_ID = "33333333-3333-3333-3333-333333333333"

ROW = {"id": USER_ID, "email": "jane@example.com", "first_name": "Jane", "last_name": "Doe", "msisdn": "+9611",
       "referral_code": "JANE1", "email_verified": True, "phone_verified": False, "should_notify": True,
       "role": None, "banned_until": None, "is_banned": False, "created_at": "2026-10-01T10:00:00+00:00",
       "last_sign_in_at": None}


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import users as routes
    fake = MagicMock(name="user-service")
    monkeypatch.setattr(routes, "service", fake)
    return fake


def _override(user):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: user
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_super_admin():
    yield from _override(make_admin(ROLE_SUPER_ADMIN))


@pytest.fixture
def as_admin():
    """A regular admin: reads everything, cannot ban."""
    yield from _override(make_admin(ROLE_ADMIN))


@pytest.fixture
def as_users_reader():
    """An admin holding only admin.users.read."""
    yield from _override(AdminUser(id="22222222-2222-2222-2222-222222222222", email="reader@example.com",
                                   role=ROLE_ADMIN, permissions=frozenset({"admin.users.read"}), token="t"))


def _failed(response, status):
    body = response.json()
    assert response.status_code == status
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    return body


def _code(response):
    return json.loads(response.json()["developerMessage"])["code"]


# ---------- list ----------

def test_list_returns_rows_total_and_facets(api_client, service, as_admin):
    banned = {**ROW, "id": "44444444-4444-4444-4444-444444444444", "is_banned": True,
              "banned_until": "2027-10-01T10:00:00+00:00"}
    service.list.return_value = AdminPage(data=[ROW, banned], total_count=2,
                                          facets={"all": 2, "banned": 1, "active": 1})

    response = api_client.get(BASE, params={"search": "ja%", "ban_status": "banned", "page": 2, "page_size": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 2
    assert body["data"]["facets"] == {"all": 2, "banned": 1, "active": 1}
    assert body["data"]["data"][1]["is_banned"] is True
    assert body["data"]["data"][0]["first_name"] == "Jane"
    search, status, page = service.list.call_args.args
    assert (search, status, page.page, page.page_size) == ("ja%", "banned", 2, 5)


def test_list_defaults(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[], total_count=0, facets={"all": 0, "banned": 0, "active": 0})

    assert api_client.get(BASE).status_code == 200
    search, status, page = service.list.call_args.args
    assert (search, status, page.page, page.page_size) == (None, "all", 1, 20)


@pytest.mark.parametrize("params", [{"page_size": 101}, {"ban_status": "foo"}, {"foo": "bar"}, {"page": 0},
                                    {"search": "x" * 101}])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)
    service.list.assert_not_called()


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)


def test_lookup_still_served_before_the_id_route(api_client, as_admin, monkeypatch):
    from app.services.admin import lookup_service
    fake = MagicMock()
    fake.users.return_value = AdminPage(data=[{"id": USER_ID, "label": "jane@example.com"}], total_count=1)
    monkeypatch.setattr(lookup_service, "service", fake)

    response = api_client.get(f"{BASE}/lookup", params={"search": "ja"})

    assert response.status_code == 200
    assert response.json()["data"]["data"][0]["label"] == "jane@example.com"


# ---------- detail ----------

def test_get_returns_row_with_wallet(api_client, service, as_admin):
    service.get.return_value = {**ROW, "wallet": {"amount": 12.5, "currency": "EUR"}}

    response = api_client.get(f"{BASE}/{USER_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["wallet"] == {"amount": 12.5, "currency": "EUR"}
    assert response.json()["data"]["email"] == "jane@example.com"
    service.get.assert_called_once_with(USER_ID)


def test_get_without_wallet(api_client, service, as_admin):
    service.get.return_value = {**ROW, "wallet": None}
    assert api_client.get(f"{BASE}/{USER_ID}").json()["data"]["wallet"] is None


def test_get_unknown_is_404(api_client, service, as_admin):
    service.get.side_effect = AdminNotFound("user_not_found")
    _failed(api_client.get(f"{BASE}/{USER_ID}"), 404)


def test_get_non_uuid_is_422(api_client, service, as_admin):
    _failed(api_client.get(f"{BASE}/not-a-uuid"), 422)
    service.get.assert_not_called()


# ---------- ban ----------

def test_ban_passes_body_and_audit_context(api_client, service, as_super_admin):
    service.ban.return_value = {"user_id": USER_ID, "email": "jane@example.com",
                                "banned_until": "2027-10-21T10:00:00+00:00", "hours_banned": 8760}

    response = api_client.post(f"{BASE}/{USER_ID}/ban", json={"reason": "  fraud  "},
                               headers={"X-Forwarded-For": "9.9.9.9"})

    assert response.status_code == 200
    assert response.json()["data"]["hours_banned"] == 8760
    user_id, body, ctx = service.ban.call_args.args
    assert user_id == USER_ID
    assert body.hours == 8760 and body.reason == "fraud"
    assert ctx.actor_id == "11111111-1111-1111-1111-111111111111"
    assert ctx.ip == "9.9.9.9"
    assert ctx.reason == "fraud"


@pytest.mark.parametrize("hours", [1, 87600])
def test_ban_accepts_boundary_hours(api_client, service, as_super_admin, hours):
    service.ban.return_value = {"user_id": USER_ID, "email": None, "banned_until": None, "hours_banned": hours}
    assert api_client.post(f"{BASE}/{USER_ID}/ban", json={"hours": hours, "reason": "fraud"}).status_code == 200
    assert service.ban.call_args.args[1].hours == hours


def test_ban_already_banned_is_409(api_client, service, as_super_admin):
    service.ban.side_effect = AdminConflict("already_banned", banned_until="2027-01-01T00:00:00+00:00")

    response = api_client.post(f"{BASE}/{USER_ID}/ban", json={"reason": "fraud"})

    _failed(response, 409)
    assert _code(response) == "already_banned"


def test_ban_unknown_user_is_404(api_client, service, as_super_admin):
    service.ban.side_effect = AdminNotFound("user_not_found")
    _failed(api_client.post(f"{BASE}/{USER_ID}/ban", json={"reason": "fraud"}), 404)


def test_ban_upstream_failure_is_502(api_client, service, as_super_admin):
    service.ban.side_effect = AdminUpstreamError("AUDIT_FAILED")
    _failed(api_client.post(f"{BASE}/{USER_ID}/ban", json={"reason": "fraud"}), 502)


@pytest.mark.parametrize("payload", [
    {"hours": 0, "reason": "fraud"},
    {"hours": 87601, "reason": "fraud"},
    {"hours": 1.5, "reason": "fraud"},
    {"hours": 1},
    {"hours": 1, "reason": ""},
    {"hours": 1, "reason": "   "},
    {"hours": 1, "reason": "x" * 501},
    {"hours": 1, "reason": "fraud", "admin_id": "me"},
])
def test_ban_invalid_body_is_422(api_client, service, as_super_admin, payload):
    _failed(api_client.post(f"{BASE}/{USER_ID}/ban", json=payload), 422)
    service.ban.assert_not_called()


def test_ban_without_ban_permission_is_403(api_client, service, as_admin):
    _failed(api_client.post(f"{BASE}/{USER_ID}/ban", json={"reason": "fraud"}), 403)
    service.ban.assert_not_called()


def test_ban_without_session_is_401(api_client, service):
    _failed(api_client.post(f"{BASE}/{USER_ID}/ban", json={"reason": "fraud"}), 401)


# ---------- unban ----------

def test_unban_passes_reason(api_client, service, as_super_admin):
    service.unban.return_value = {"user_id": USER_ID, "email": "jane@example.com"}

    response = api_client.post(f"{BASE}/{USER_ID}/unban", json={"reason": "cleared"})

    assert response.status_code == 200
    assert response.json()["data"] == {"user_id": USER_ID, "email": "jane@example.com"}
    user_id, body, ctx = service.unban.call_args.args
    assert user_id == USER_ID and body.reason == "cleared" and ctx.reason == "cleared"


def test_unban_not_banned_is_409(api_client, service, as_super_admin):
    service.unban.side_effect = AdminConflict("not_banned")

    response = api_client.post(f"{BASE}/{USER_ID}/unban", json={"reason": "fraud"})

    _failed(response, 409)
    assert _code(response) == "not_banned"


@pytest.mark.parametrize("payload", [{}, {"reason": ""}, {"reason": "fraud", "hours": 1}])
def test_unban_invalid_body_is_422(api_client, service, as_super_admin, payload):
    _failed(api_client.post(f"{BASE}/{USER_ID}/unban", json=payload), 422)
    service.unban.assert_not_called()


def test_unban_without_ban_permission_is_403(api_client, service, as_admin):
    _failed(api_client.post(f"{BASE}/{USER_ID}/unban", json={"reason": "fraud"}), 403)
    service.unban.assert_not_called()


# ---------- sub-lists ----------

def test_devices_sub_list(api_client, service, as_admin):
    service.list_devices.return_value = AdminPage(data=[{"id": 5, "user_id": USER_ID, "device_model": "iPhone"}],
                                                  total_count=1)

    response = api_client.get(f"{BASE}/{USER_ID}/devices", params={"page": 2, "page_size": 10})

    assert response.status_code == 200
    assert response.json()["totalCount"] == 1
    assert response.json()["data"]["data"][0]["device_model"] == "iPhone"
    service.list_devices.assert_called_once_with(USER_ID, PageParams(page=2, page_size=10))


def test_devices_need_devices_read(api_client, service, as_users_reader):
    _failed(api_client.get(f"{BASE}/{USER_ID}/devices"), 403)
    service.list_devices.assert_not_called()


def test_wallet_transactions_sub_list(api_client, service, as_users_reader):
    row = {"id": "t1", "wallet_id": "w1", "amount": 5, "status": "success", "source": "Top up", "currency": "USD",
           "created_at": "2026-10-01T10:00:00"}
    service.list_wallet_transactions.return_value = AdminPage(data=[row], total_count=42)

    response = api_client.get(f"{BASE}/{USER_ID}/wallet-transactions")

    assert response.status_code == 200
    assert response.json()["data"]["total_count"] == 42
    assert response.json()["data"]["data"][0]["amount"] == 5
    service.list_wallet_transactions.assert_called_once_with(USER_ID, PageParams(page=1, page_size=20))


def test_wallet_transactions_reject_unknown_param(api_client, service, as_admin):
    _failed(api_client.get(f"{BASE}/{USER_ID}/wallet-transactions", params={"search": "x"}), 422)


def test_referrals_sub_list_passes_filters(api_client, service, as_admin):
    row = {"id": "o1", "referral_code": "JANE1", "amount": 2, "currency": "USD", "from_user_email": "a@x.com",
           "to_user_email": "b@x.com", "created_at": "2026-10-01T10:00:00", "payment_time": None}
    service.list_referrals.return_value = AdminPage(data=[row], total_count=1)

    response = api_client.get(f"{BASE}/{USER_ID}/referrals",
                              params={"search": "JANE", "from_email": "a@", "date": "2026-10-01", "page_size": 5})

    assert response.status_code == 200
    assert response.json()["data"]["data"][0]["from_user_email"] == "a@x.com"
    service.list_referrals.assert_called_once_with(USER_ID, PageParams(page=1, page_size=5), search="JANE",
                                                   from_email="a@", referral_date=date(2026, 10, 1))


def test_referrals_bad_date_is_422(api_client, service, as_admin):
    _failed(api_client.get(f"{BASE}/{USER_ID}/referrals", params={"date": "yesterday"}), 422)


def test_referrals_need_referrals_read(api_client, service, as_users_reader):
    _failed(api_client.get(f"{BASE}/{USER_ID}/referrals"), 403)
    service.list_referrals.assert_not_called()
