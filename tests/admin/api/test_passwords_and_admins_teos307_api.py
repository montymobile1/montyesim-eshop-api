"""TEOS-307: HTTP contract of the password routes and the Admins page routes."""
from types import SimpleNamespace

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminConflict, AdminError
from app.schemas.admin.admins import AdminAccessRevoked, AdminPasswordResetSent
from app.schemas.admin.auth import AdminPasswordChanged, AdminPasswordReset, AdminProfile
from app.schemas.admin.common import AdminPage
from tests.admin.conftest import make_admin

AUTH = "/api/v1/admin/auth"
ADMINS = "/api/v1/admin/admins"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
GOOD = "N3w-Passw0rd!x"


@pytest.fixture
def as_role():
    from app.main import esim_app

    def set_role(role):
        esim_app.dependency_overrides[admin_user] = lambda: make_admin(role)

    yield set_role
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def auth_service(monkeypatch):
    from app.api.v1.admin import auth as routes
    fake = SimpleNamespace(calls=[])
    monkeypatch.setattr(routes, "service", fake)
    return fake


@pytest.fixture
def accounts(monkeypatch):
    from app.api.v1.admin import admins as routes
    fake = SimpleNamespace(calls=[])
    monkeypatch.setattr(routes, "service", fake)
    return fake


def test_change_password_returns_the_new_session(api_client, as_role, auth_service):
    as_role(ROLE_ADMIN)
    auth_service.change_password = lambda user, current, new, ip, ctx: auth_service.calls.append(
        (user.id, current, new, ctx.actor_id)) or AdminPasswordChanged(
        access_token="a2", refresh_token="r2", expires_in=3600, other_sessions_signed_out=True,
        user=AdminProfile(id=user.id, email=user.email, role=user.role, permissions=[]))

    response = api_client.post(f"{AUTH}/change-password", json={"current_password": "old", "new_password": GOOD})

    assert response.status_code == 200
    assert response.json()["data"]["access_token"] == "a2"
    assert response.json()["data"]["other_sessions_signed_out"] is True
    assert auth_service.calls[0][1:3] == ("old", GOOD)


@pytest.mark.parametrize("new_password", ["short1!A", "alllowercase1!x", "NoSymbolHere123"])
def test_change_password_rules_are_422(api_client, as_role, auth_service, new_password):
    as_role(ROLE_ADMIN)

    response = api_client.post(f"{AUTH}/change-password", json={"current_password": "old", "new_password": new_password})

    assert response.status_code == 422
    assert response.json()["developerMessage"].startswith("new_password: ")
    assert auth_service.calls == []


def test_change_password_wrong_current_is_400_with_a_translated_message(api_client, as_role, auth_service):
    as_role(ROLE_ADMIN)

    def wrong(*args):
        from app.config.constants import ErrorMessages
        raise AdminError("current_password_invalid", name=ErrorMessages.ADMIN_CURRENT_PASSWORD_INVALID)

    auth_service.change_password = wrong

    response = api_client.post(f"{AUTH}/change-password", json={"current_password": "x", "new_password": GOOD})

    assert response.status_code == 400
    assert response.json()["message"] == "The current password is incorrect."
    assert response.json()["developerMessage"] == "current_password_invalid"


def test_change_password_needs_a_session(api_client, auth_service):
    response = api_client.post(f"{AUTH}/change-password", json={"current_password": "x", "new_password": GOOD})

    assert response.status_code == 401


def test_reset_password_is_public(api_client, auth_service):
    auth_service.complete_password_reset = lambda token, new, ip: auth_service.calls.append(
        (token, new)) or AdminPasswordReset(sessions_signed_out=True)

    response = api_client.post(f"{AUTH}/reset-password", json={"token": "link-token", "new_password": GOOD})

    assert response.status_code == 200
    assert response.json()["data"] == {"sessions_signed_out": True}
    assert auth_service.calls == [("link-token", GOOD)]


@pytest.mark.parametrize("method, path, body", [
    ("get", "", None), ("post", f"/{OTHER_ID}/reset-password", None),
    ("post", f"/{OTHER_ID}/revoke-access", {"reason": "left the company"})])
def test_admins_routes_are_super_admin_only(api_client, as_role, accounts, method, path, body):
    as_role(ROLE_ADMIN)

    response = getattr(api_client, method)(f"{ADMINS}{path}", **({"json": body} if body else {}))

    assert response.status_code == 403
    assert accounts.calls == []


def test_admins_list(api_client, as_role, accounts):
    as_role(ROLE_SUPER_ADMIN)
    accounts.list = lambda search, role, page, uid: accounts.calls.append((search, role, page.page, uid)) or AdminPage(
        data=[], total_count=0, facets={"all": 0, "super_admin": 0, "admin": 0})

    response = api_client.get(f"{ADMINS}?search=amy&role=admin&page=2&page_size=10")

    assert response.status_code == 200
    assert response.json()["data"]["facets"] == {"all": 0, "super_admin": 0, "admin": 0}
    assert accounts.calls == [("amy", "admin", 2, make_admin().id)]


def test_admins_list_unknown_role_is_422(api_client, as_role, accounts):
    as_role(ROLE_SUPER_ADMIN)

    assert api_client.get(f"{ADMINS}?role=owner").status_code == 422


def test_reset_password_link_is_sent(api_client, as_role, accounts):
    as_role(ROLE_SUPER_ADMIN)
    accounts.send_password_reset = lambda admin_id, ctx: AdminPasswordResetSent(id=admin_id, email="amy@x.com")

    response = api_client.post(f"{ADMINS}/{OTHER_ID}/reset-password")

    assert response.status_code == 200
    assert response.json()["data"] == {"id": OTHER_ID, "email": "amy@x.com"}


def test_revoke_access_needs_a_reason(api_client, as_role, accounts):
    as_role(ROLE_SUPER_ADMIN)

    assert api_client.post(f"{ADMINS}/{OTHER_ID}/revoke-access", json={"reason": "  a "}).status_code == 422
    assert api_client.post(f"{ADMINS}/{OTHER_ID}/revoke-access", json={}).status_code == 422


def test_revoke_access_passes_the_reason_and_returns_the_result(api_client, as_role, accounts):
    as_role(ROLE_SUPER_ADMIN)
    accounts.revoke_access = lambda admin_id, ctx: accounts.calls.append(ctx.reason) or AdminAccessRevoked(
        id=admin_id, email="amy@x.com", previous_role=ROLE_ADMIN, sessions_signed_out=True)

    response = api_client.post(f"{ADMINS}/{OTHER_ID}/revoke-access", json={"reason": " left the company "})

    assert response.status_code == 200
    assert response.json()["data"]["previous_role"] == ROLE_ADMIN
    assert accounts.calls == ["left the company"]


def test_revoke_own_access_is_409(api_client, as_role, accounts):
    as_role(ROLE_SUPER_ADMIN)

    def refuse(admin_id, ctx):
        from app.config.constants import ErrorMessages
        raise AdminConflict("cannot_target_self", name=ErrorMessages.ADMIN_CANNOT_TARGET_SELF)

    accounts.revoke_access = refuse

    response = api_client.post(f"{ADMINS}/{OTHER_ID}/revoke-access", json={"reason": "testing self"})

    assert response.status_code == 409
    assert response.json()["message"] == "You can't do this on your own account."
