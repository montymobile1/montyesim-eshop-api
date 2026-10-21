"""TEOS-111 / TEOS-115 HTTP contract: users sort, ban of self, session revoke failure, and the
devices privacy, filters and facets. The real services and repositories run; only the database
calls (``paged``, the facet counts, ``by_ids``) and Supabase Auth are replaced."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.repo.admin.base import Paged
from app.repo.admin.device_repo import DeviceAdminRepo
from app.schemas.admin.common import AdminPage
from app.services.admin.device_service import DeviceAdminService
from app.services.admin.user_service import UserAdminService
from tests.admin.conftest import make_admin

USERS = "/api/v1/admin/users"
DEVICES = "/api/v1/admin/devices"
ADMIN_ID = "11111111-1111-1111-1111-111111111111"  # make_admin's id
USER_ID = "33333333-3333-3333-3333-333333333333"
SECRET_FIELDS = ("fcm_token", "originated_ip")
RAW_DEVICE = {"id": 7, "user_id": USER_ID, "device_id": "d-7", "manufacturer": "Apple", "device_model": "iPhone",
              "os": "iOS", "is_logged_in": True, "fcm_token": "push-token-secret", "originated_ip": "203.0.113.9",
              "ip_location": "Beirut", "created_at": "2026-10-01T10:00:00"}
ANONYMOUS_DEVICE = {**RAW_DEVICE, "id": 8, "user_id": None}


def _as(role):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(role)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_admin():
    yield from _as(ROLE_ADMIN)


@pytest.fixture
def as_super_admin():
    yield from _as(ROLE_SUPER_ADMIN)


@pytest.fixture
def device_repo(monkeypatch):
    """A real DeviceAdminRepo whose page query returns raw rows (secret columns included)."""
    repo = DeviceAdminRepo()
    seen = {}

    def fake_paged(query, page, order=None):
        seen.update(params=dict(query.params.multi_items()), order=list(order) if order else None)
        return Paged(rows=[dict(RAW_DEVICE), dict(ANONYMOUS_DEVICE)], total=2)

    def fake_count(**kwargs):
        seen.setdefault("counts", []).append(kwargs)
        return {"all": 9, "android": 5, "ios": 4}[kwargs["platform"]]

    monkeypatch.setattr(repo, "paged", fake_paged)
    monkeypatch.setattr(repo, "_count", fake_count)
    repo.seen = seen
    return repo


@pytest.fixture
def users_repo():
    users = MagicMock(name="users-copy")
    users.by_ids.return_value = {USER_ID: {"id": USER_ID, "email": "jane@example.com", "msisdn": None}}
    return users


@pytest.fixture
def devices_route(monkeypatch, device_repo, users_repo):
    from app.api.v1.admin import devices as routes
    monkeypatch.setattr(routes, "service", DeviceAdminService(devices=device_repo, users=users_repo))
    return device_repo


def _assert_no_secret(body):
    text = json.dumps(body)
    for field in SECRET_FIELDS:
        assert field not in text
    assert "push-token-secret" not in text and "203.0.113.9" not in text


# ---------- TEOS-115 case 16: never fcm_token or originated_ip ----------

def test_devices_list_never_returns_push_token_or_ip(api_client, as_admin, devices_route):
    response = api_client.get(DEVICES)

    assert response.status_code == 200
    _assert_no_secret(response.json())
    row = response.json()["data"]["data"][0]
    assert row["device_model"] == "iPhone" and row["ip_location"] == "Beirut"


def test_user_devices_never_return_push_token_or_ip(api_client, as_admin, monkeypatch, device_repo, users_repo):
    from app.api.v1.admin import users as routes
    monkeypatch.setattr(routes, "service", UserAdminService(
        repo=MagicMock(), devices=DeviceAdminService(devices=device_repo, users=users_repo)))

    response = api_client.get(f"{USERS}/{USER_ID}/devices")

    assert response.status_code == 200
    _assert_no_secret(response.json())
    assert device_repo.seen["params"]["user_id"] == f"eq.{USER_ID}"


# ---------- TEOS-115: owner email, facets, filters, sort ----------

def test_devices_rows_carry_the_owner_email_and_platform_facets(api_client, as_admin, devices_route, users_repo):
    body = api_client.get(DEVICES).json()["data"]

    assert [row["user_email"] for row in body["data"]] == ["jane@example.com", None]
    assert body["facets"] == {"all": 9, "android": 5, "ios": 4}
    assert body["total_count"] == 2
    users_repo.by_ids.assert_called_once_with([USER_ID])


def test_devices_defaults_newest_first_without_filters(api_client, as_admin, devices_route):
    api_client.get(DEVICES)

    assert devices_route.seen["params"] == {"select": "*"}
    assert devices_route.seen["order"] is None  # paged() default: created_at desc, id desc


def test_devices_filters_are_sent_and_facets_ignore_the_platform(api_client, as_admin, devices_route):
    response = api_client.get(DEVICES, params={"platform": "android", "logged_in": "no", "user_id": USER_ID})

    assert response.status_code == 200
    params = devices_route.seen["params"]
    assert params["os"] == "ilike.android"
    assert params["is_logged_in"] == "eq.False"
    assert params["user_id"] == f"eq.{USER_ID}"
    # every facet count keeps user_id and logged_in, and counts each platform
    counts = devices_route.seen["counts"]
    assert {c["platform"] for c in counts} == {"all", "android", "ios"}
    assert all(c["logged_in"] == "no" and c["user_id"] == USER_ID for c in counts)


def test_devices_logged_in_yes(api_client, as_admin, devices_route):
    api_client.get(DEVICES, params={"logged_in": "yes", "platform": "ios"})
    assert devices_route.seen["params"]["is_logged_in"] == "eq.True"
    assert devices_route.seen["params"]["os"] == "ilike.ios"


def test_devices_sort_by_last_login_ascending_with_tie_breakers(api_client, as_admin, devices_route):
    api_client.get(DEVICES, params={"sort": "timestamp_login", "dir": "asc"})
    assert devices_route.seen["order"] == [("timestamp_login", False), ("created_at", True), ("id", False)]


def test_devices_sort_created_ascending(api_client, as_admin, devices_route):
    api_client.get(DEVICES, params={"sort": "created_at", "dir": "asc"})
    assert devices_route.seen["order"] == [("created_at", False), ("id", False)]


@pytest.mark.parametrize("params", [
    {"platform": "windows"}, {"logged_in": "maybe"}, {"sort": "fcm_token"}, {"sort": "originated_ip"},
    {"dir": "up"}, {"direction": "asc"}, {"user_id": "not-a-uuid"}, {"page_size": 1000},
])
def test_devices_bad_query_is_422(api_client, as_admin, devices_route, params):
    response = api_client.get(DEVICES, params=params)
    assert response.status_code == 422
    assert "params" not in devices_route.seen


def test_devices_empty_result(api_client, as_admin, monkeypatch, users_repo):
    from app.api.v1.admin import devices as routes
    repo = MagicMock()
    repo.search.return_value = Paged(rows=[], total=0)
    repo.platform_facets.return_value = {"all": 0, "android": 0, "ios": 0}
    monkeypatch.setattr(routes, "service", DeviceAdminService(devices=repo, users=users_repo))

    body = api_client.get(DEVICES).json()["data"]

    assert body == {"data": [], "total_count": 0, "facets": {"all": 0, "android": 0, "ios": 0}, "totals": None}
    users_repo.by_ids.assert_not_called()


# ---------- TEOS-111: users sort ----------

@pytest.fixture
def users_service(monkeypatch):
    from app.api.v1.admin import users as routes
    fake = MagicMock(name="user-service")
    fake.list.return_value = AdminPage(data=[], total_count=0, facets={"all": 0, "banned": 0, "active": 0})
    monkeypatch.setattr(routes, "service", fake)
    return fake


def test_users_sort_defaults_to_created_desc(api_client, as_admin, users_service):
    assert api_client.get(USERS).status_code == 200
    assert users_service.list.call_args.kwargs == {"sort": "created_at", "direction": "desc"}


def test_users_sort_by_email_ascending(api_client, as_admin, users_service):
    assert api_client.get(USERS, params={"sort": "email", "dir": "asc"}).status_code == 200
    assert users_service.list.call_args.kwargs == {"sort": "email", "direction": "asc"}


@pytest.mark.parametrize("params", [{"sort": "password"}, {"sort": "banned_until"}, {"dir": "sideways"},
                                    {"direction": "asc"}])
def test_users_bad_sort_is_422(api_client, as_admin, users_service, params):
    assert api_client.get(USERS, params=params).status_code == 422
    users_service.list.assert_not_called()


# ---------- TEOS-111: ban of self, session revoke failure ----------

@pytest.fixture
def real_users(monkeypatch):
    from app.api.v1.admin import users as routes
    repo, audit, client, revoker = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    repo.get.return_value = {"id": USER_ID, "email": "jane@example.com", "is_banned": False, "banned_until": None}
    client.auth.admin.update_user_by_id.return_value = SimpleNamespace(
        user=SimpleNamespace(email="jane@example.com", banned_until="2026-10-22T10:00:00+00:00"))
    monkeypatch.setattr(routes, "service", UserAdminService(repo=repo, audit=audit, client_factory=lambda: client,
                                                            session_revoker=revoker))
    return {"repo": repo, "audit": audit, "client": client, "revoker": revoker}


@pytest.mark.parametrize("action, body", [("ban", {"hours": 24, "reason": "test"}), ("unban", {"reason": "test"})])
def test_ban_or_unban_self_is_409_cannot_ban_self(api_client, as_super_admin, real_users, action, body):
    response = api_client.post(f"{USERS}/{ADMIN_ID}/{action}", json=body)

    assert response.status_code == 409
    assert json.loads(response.json()["developerMessage"])["code"] == "cannot_ban_self"
    real_users["repo"].get.assert_not_called()
    real_users["client"].auth.admin.update_user_by_id.assert_not_called()
    real_users["audit"].log.assert_not_called()


def test_ban_session_revoke_failure_is_502_session_revoke_failed(api_client, as_super_admin, real_users):
    real_users["revoker"].side_effect = RuntimeError("gotrue down")

    response = api_client.post(f"{USERS}/{USER_ID}/ban", json={"hours": 24, "reason": "fraud"})

    assert response.status_code == 502
    assert response.json()["developerMessage"] == "session_revoke_failed"
    real_users["client"].auth.admin.update_user_by_id.assert_called_once_with(USER_ID, {"ban_duration": "24h"})
    assert real_users["audit"].log.call_args.kwargs["sessions_revoked"] is False


def test_ban_revokes_sessions(api_client, as_super_admin, real_users):
    response = api_client.post(f"{USERS}/{USER_ID}/ban", json={"hours": 24, "reason": "fraud"})

    assert response.status_code == 200
    real_users["revoker"].assert_called_once_with(USER_ID)
