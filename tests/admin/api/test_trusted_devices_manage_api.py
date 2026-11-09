"""TEOS-255 (BE-2): HTTP contract of /me/devices and /admins/{id}/devices (services faked)."""
import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.config.constants import ErrorMessages
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminNotFound, coded
from app.schemas.admin.trusted_devices import AdminDeviceRow
from app.services.admin.trusted_device_service import REASON_SIGN_OUT_OTHERS, REASON_SUPER_ADMIN, REASON_USER
from tests.admin.conftest import make_admin

ME = "/api/v1/admin/me/devices"
ADMINS = "/api/v1/admin/admins"
CALLER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
DEVICE_ID = "33333333-3333-3333-3333-333333333333"
MY_DEVICE = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)


def row(device_id=DEVICE_ID, current=False, name=None):
    return AdminDeviceRow(id=device_id, name=name, browser="Chrome", os="Windows", created_at=NOW,
                          last_used_at=NOW, trusted=True, trusted_until=NOW, signed_in=True, current=current)


def not_found():
    return coded(AdminNotFound, "device_not_found", ErrorMessages.ADMIN_DEVICE_NOT_FOUND)


@pytest.fixture
def as_role():
    from app.main import esim_app

    def set_role(role, session_id="me-session", device_id=MY_DEVICE):
        user = dataclasses.replace(make_admin(role), session_id=session_id, device_id=device_id)
        esim_app.dependency_overrides[admin_user] = lambda: user

    yield set_role
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def devices(monkeypatch):
    """One fake TrustedDeviceService used by both route files."""
    from app.api.v1.admin import admins as admin_routes
    from app.api.v1.admin import me_devices as me_routes
    fake = SimpleNamespace(calls=[])
    monkeypatch.setattr(me_routes, "service", fake)
    monkeypatch.setattr(admin_routes, "devices", fake)
    return fake


@pytest.fixture
def accounts(monkeypatch):
    from app.api.v1.admin import admins as admin_routes
    fake = SimpleNamespace(calls=[])
    fake.admin_account = lambda admin_id: fake.calls.append(admin_id) or SimpleNamespace(id=admin_id)
    monkeypatch.setattr(admin_routes, "service", fake)
    return fake


# ---------- case 5: GET /me/devices ----------

def test_case5_my_devices_list_with_the_current_row(api_client, as_role, devices):
    as_role(ROLE_ADMIN)
    devices.list_devices = lambda admin_id, session_id: devices.calls.append((admin_id, session_id)) or [
        row(MY_DEVICE, current=True), row(DEVICE_ID)]

    response = api_client.get(ME)

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["total_count"] == 2
    assert body["data"]["data"][0]["current"] is True and body["data"]["data"][1]["current"] is False
    assert devices.calls == [(CALLER_ID, "me-session")]
    assert "device_hash" not in body["data"]["data"][0] and "session_id" not in body["data"]["data"][0]


def test_my_devices_needs_a_signed_in_admin(api_client):
    assert api_client.get(ME).status_code == 401


# ---------- case 6: rename ----------

def test_case6_rename_my_device(api_client, as_role, devices):
    as_role(ROLE_ADMIN)

    def rename(admin_id, device_id, name, ctx, current_session_id=None):
        devices.calls.append((admin_id, device_id, name, ctx.actor_id, current_session_id))
        return row(device_id, name=name)

    devices.rename = rename

    response = api_client.patch(f"{ME}/{DEVICE_ID}", json={"name": "  Work   laptop "})

    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Work laptop"
    assert devices.calls == [(CALLER_ID, DEVICE_ID, "Work laptop", CALLER_ID, "me-session")]


@pytest.mark.parametrize("body", [{"name": "   "}, {"name": "x" * 61}, {}, {"name": "ok", "extra": 1}])
def test_rename_rules_are_422(api_client, as_role, devices, body):
    as_role(ROLE_ADMIN)
    devices.rename = lambda *a, **k: devices.calls.append(a)

    response = api_client.patch(f"{ME}/{DEVICE_ID}", json=body)

    assert response.status_code == 422
    assert devices.calls == []


# ---------- case 23: another admin's device id ----------

def test_case23_rename_or_revoke_a_device_that_is_not_mine_is_404(api_client, as_role, devices):
    as_role(ROLE_ADMIN)

    def missing(*args, **kwargs):
        raise not_found()

    devices.rename = missing
    devices.revoke = missing

    for response in (api_client.patch(f"{ME}/{DEVICE_ID}", json={"name": "x"}),
                     api_client.delete(f"{ME}/{DEVICE_ID}")):
        assert response.status_code == 404
        assert '"code": "device_not_found"' in response.json()["developerMessage"]


def test_device_id_must_be_a_uuid(api_client, as_role, devices):
    as_role(ROLE_ADMIN)
    devices.revoke = lambda *a: devices.calls.append(a)

    assert api_client.delete(f"{ME}/not-a-uuid").status_code == 422
    assert devices.calls == []


# ---------- case 7: revoke my device ----------

def test_case7_revoke_my_device_is_204(api_client, as_role, devices):
    as_role(ROLE_ADMIN)
    devices.revoke = lambda admin_id, device_id, reason, ctx: devices.calls.append(
        (admin_id, device_id, reason, ctx.actor_id))

    response = api_client.delete(f"{ME}/{MY_DEVICE}")

    assert response.status_code == 204 and response.content == b""
    assert devices.calls == [(CALLER_ID, MY_DEVICE, REASON_USER, CALLER_ID)]


# ---------- case 8: sign out all other devices ----------

def test_case8_sign_out_all_other_devices_keeps_mine(api_client, as_role, devices):
    as_role(ROLE_ADMIN)
    devices.revoke_all = lambda admin_id, reason, ctx, except_device_id=None: devices.calls.append(
        (admin_id, reason, except_device_id)) or 3

    response = api_client.delete(ME, params={"except": "current"})

    assert response.status_code == 200
    assert response.json()["data"] == {"revoked": 3}
    assert devices.calls == [(CALLER_ID, REASON_SIGN_OUT_OTHERS, MY_DEVICE)]


def test_sign_out_others_finds_my_device_by_session_when_the_user_has_no_device_id(api_client, as_role, devices):
    as_role(ROLE_ADMIN, device_id=None)
    devices.session_device = lambda session_id: {"id": "55555555-5555-5555-5555-555555555555",
                                                 "revoked_at": None} if session_id == "me-session" else None
    devices.revoke_all = lambda admin_id, reason, ctx, except_device_id=None: devices.calls.append(
        except_device_id) or 1

    assert api_client.delete(ME, params={"except": "current"}).status_code == 200
    assert devices.calls == ["55555555-5555-5555-5555-555555555555"]


@pytest.mark.parametrize("params", [{}, {"except": "all"}, {"except": ""}])
def test_sign_out_others_needs_except_current(api_client, as_role, devices, params):
    as_role(ROLE_ADMIN)
    devices.revoke_all = lambda *a, **k: devices.calls.append(a)

    response = api_client.delete(ME, params=params)

    assert response.status_code == 422
    assert devices.calls == []


# ---------- case 9 / 24: another admin's devices ----------

def test_case9_super_admin_lists_another_admins_devices(api_client, as_role, devices, accounts):
    as_role(ROLE_SUPER_ADMIN)
    devices.list_devices = lambda admin_id, session_id: devices.calls.append((admin_id, session_id)) or [row()]

    response = api_client.get(f"{ADMINS}/{OTHER_ID}/devices")

    assert response.status_code == 200
    assert response.json()["data"]["total_count"] == 1
    assert response.json()["data"]["data"][0]["current"] is False
    assert accounts.calls == [OTHER_ID]
    assert devices.calls == [(OTHER_ID, "me-session")]


def test_case9_super_admin_revokes_another_admins_device(api_client, as_role, devices, accounts):
    as_role(ROLE_SUPER_ADMIN)
    devices.revoke = lambda admin_id, device_id, reason, ctx: devices.calls.append(
        (admin_id, device_id, reason, ctx.actor_id))

    response = api_client.delete(f"{ADMINS}/{OTHER_ID}/devices/{DEVICE_ID}")

    assert response.status_code == 204
    assert devices.calls == [(OTHER_ID, DEVICE_ID, REASON_SUPER_ADMIN, CALLER_ID)]


def test_super_admin_revoking_a_device_of_another_admin_that_is_not_theirs_is_404(api_client, as_role, devices,
                                                                                  accounts):
    as_role(ROLE_SUPER_ADMIN)

    def missing(*args):
        raise not_found()

    devices.revoke = missing

    response = api_client.delete(f"{ADMINS}/{OTHER_ID}/devices/{DEVICE_ID}")

    assert response.status_code == 404
    assert '"code": "device_not_found"' in response.json()["developerMessage"]


def test_devices_of_an_unknown_admin_is_404(api_client, as_role, devices, accounts):
    as_role(ROLE_SUPER_ADMIN)

    def unknown(admin_id):
        raise AdminNotFound("admin_not_found", name=ErrorMessages.ADMIN_ACCOUNT_NOT_FOUND)

    accounts.admin_account = unknown
    devices.list_devices = lambda *a: devices.calls.append(a) or []

    response = api_client.get(f"{ADMINS}/{OTHER_ID}/devices")

    assert response.status_code == 404
    assert response.json()["developerMessage"] == "admin_not_found"
    assert devices.calls == []


def test_case24_admin_without_permission_gets_403(api_client, as_role, devices, accounts):
    as_role(ROLE_ADMIN)
    devices.list_devices = lambda *a: devices.calls.append(a) or []
    devices.revoke = lambda *a: devices.calls.append(a)

    assert api_client.get(f"{ADMINS}/{OTHER_ID}/devices").status_code == 403
    assert api_client.delete(f"{ADMINS}/{OTHER_ID}/devices/{DEVICE_ID}").status_code == 403
    assert devices.calls == [] and accounts.calls == []


def test_routes_are_in_the_openapi_spec(api_client):
    paths = api_client.get("/openapi.json").json()["paths"]

    assert {"get", "delete"} <= set(paths["/api/v1/admin/me/devices"])
    assert {"patch", "delete"} <= set(paths["/api/v1/admin/me/devices/{device_id}"])
    assert "get" in paths["/api/v1/admin/admins/{admin_id}/devices"]
    assert "delete" in paths["/api/v1/admin/admins/{admin_id}/devices/{device_id}"]
