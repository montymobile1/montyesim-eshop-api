"""HTTP contract of /api/v1/admin/auth/* and of the shared admin route behaviour."""
from types import SimpleNamespace

import jwt
import pytest
from gotrue.errors import AuthApiError
from starlette.requests import Request

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN, permissions_for
from app.dependencies.admin_security import client_ip, require_permission
from app.exceptions.admin import AdminForbidden, AdminUnauthorized
from app.schemas.admin.auth import AdminProfile, AdminSession
from tests.admin.conftest import make_admin, make_auth_user

BASE = "/api/v1/admin/auth"
BEARER = {"Authorization": "Bearer access-token"}


def _session(role=ROLE_ADMIN) -> AdminSession:
    return AdminSession(access_token="access-token", refresh_token="refresh-token", expires_in=3600,
                        user=AdminProfile(id="u1", email="admin@example.com", role=role,
                                          permissions=sorted(permissions_for(role))))


@pytest.fixture
def auth_service(monkeypatch):
    from app.api.v1.admin import auth as auth_routes
    fake = SimpleNamespace(calls=[])
    monkeypatch.setattr(auth_routes, "service", fake)
    return fake


def _assert_failed(response, status, developer_message=None):
    body = response.json()
    assert response.status_code == status
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    if developer_message is not None:
        assert body["developerMessage"] == developer_message
    return body


# ---------- login / refresh ----------

def test_login_returns_the_session_in_the_standard_envelope(api_client, auth_service):
    auth_service.login = lambda email, password, ip: auth_service.calls.append((email, password, ip)) or _session()

    response = api_client.post(f"{BASE}/login", json={"email": "admin@example.com", "password": "secret"},
                               headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["access_token"] == "access-token"
    assert body["data"]["user"]["role"] == ROLE_ADMIN
    assert "admin.settings.read" not in body["data"]["user"]["permissions"]
    assert auth_service.calls == [("admin@example.com", "secret", "9.9.9.9")]


def test_failed_login_is_401_with_machine_code(api_client, auth_service):
    def fail(*_):
        raise AdminUnauthorized("ADMIN_LOGIN_FAILED", name="ADMIN_LOGIN_FAILED")
    auth_service.login = fail

    response = api_client.post(f"{BASE}/login", json={"email": "admin@example.com", "password": "nope"})

    body = _assert_failed(response, 401, "ADMIN_LOGIN_FAILED")
    assert body["message"] == "Invalid email or password."


@pytest.mark.parametrize("payload", [
    {"email": "admin@example.com"},
    {"email": "not-an-email", "password": "x"},
    {"email": "admin@example.com", "password": ""},
    {"email": "admin@example.com", "password": "x", "role": "super_admin"},
])
def test_invalid_login_body_is_422_with_http_422(api_client, auth_service, payload):
    response = api_client.post(f"{BASE}/login", json=payload)

    _assert_failed(response, 422)


def test_unknown_query_parameter_is_422(api_client, auth_service):
    auth_service.login = lambda *_: _session()

    response = api_client.post(f"{BASE}/login?debug=1", json={"email": "admin@example.com", "password": "x"})

    body = _assert_failed(response, 422)
    assert "debug" in body["developerMessage"]


def test_oversized_json_body_is_413(api_client, auth_service, monkeypatch):
    monkeypatch.setenv("ADMIN_MAX_JSON_BYTES", "50")

    response = api_client.post(f"{BASE}/login", json={"email": "admin@example.com", "password": "x" * 100})

    _assert_failed(response, 413)


def test_refresh_returns_a_new_session(api_client, auth_service):
    auth_service.refresh = lambda token: auth_service.calls.append(token) or _session(ROLE_SUPER_ADMIN)

    response = api_client.post(f"{BASE}/refresh", json={"refresh_token": "refresh-token"})

    assert response.status_code == 200
    assert response.json()["data"]["user"]["role"] == ROLE_SUPER_ADMIN
    assert auth_service.calls == ["refresh-token"]


# ---------- bearer-protected routes (real admin_security, Supabase mocked) ----------

def test_me_without_token_is_401(api_client):
    _assert_failed(api_client.get(f"{BASE}/me"), 401, "BEARER_TOKEN_REQUIRED")


def test_me_with_rejected_token_is_401(api_client, supabase_auth):
    supabase_auth.get_user.side_effect = AuthApiError("invalid JWT", 401, "bad_jwt")

    _assert_failed(api_client.get(f"{BASE}/me", headers=BEARER), 401, "INVALID_OR_EXPIRED_TOKEN")


def test_me_for_an_account_without_admin_role_is_403_not_401(api_client, supabase_auth):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(role=None))

    _assert_failed(api_client.get(f"{BASE}/me", headers=BEARER), 403, "ADMIN_ROLE_REQUIRED")


def test_me_returns_current_role_and_permissions(api_client, supabase_auth):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(role=ROLE_SUPER_ADMIN))

    response = api_client.get(f"{BASE}/me", headers=BEARER)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["role"] == ROLE_SUPER_ADMIN
    assert data["permissions"] == sorted(permissions_for(ROLE_SUPER_ADMIN))
    supabase_auth.get_user.assert_called_once_with(jwt="access-token")


def test_token_signed_with_another_secret_is_rejected_before_supabase(api_client, supabase_auth, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "the-real-secret")
    forged = jwt.encode({"sub": "u1", "aud": "authenticated", "exp": 4102444800}, "another-secret", algorithm="HS256")

    response = api_client.get(f"{BASE}/me", headers={"Authorization": f"Bearer {forged}"})

    _assert_failed(response, 401)
    supabase_auth.get_user.assert_not_called()


def test_logout_is_204_with_empty_body(api_client, supabase_auth, monkeypatch):
    from app.api.v1.admin import auth as auth_routes
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(role=ROLE_ADMIN))
    calls = []
    monkeypatch.setattr(auth_routes.service, "logout", lambda user: calls.append(user.token))

    response = api_client.post(f"{BASE}/logout", headers=BEARER)

    assert response.status_code == 204
    assert response.content == b""
    assert calls == ["access-token"]


# ---------- require_permission / client_ip ----------

def test_require_permission_refuses_an_admin_on_a_super_admin_permission():
    check = require_permission("admin.settings.write")

    with pytest.raises(AdminForbidden) as err:
        check(user=make_admin(ROLE_ADMIN))

    assert err.value.code == 403
    assert err.value.details == "admin.settings.write"
    assert check(user=make_admin(ROLE_SUPER_ADMIN)).role == ROLE_SUPER_ADMIN


def _request(headers, client=("10.0.0.9", 1234)):
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "client": client})


@pytest.mark.parametrize("headers, expected", [
    ({"CF-Connecting-IP": "1.1.1.1", "X-Real-IP": "2.2.2.2", "X-Forwarded-For": "3.3.3.3"}, "1.1.1.1"),
    ({"X-Real-IP": "2.2.2.2", "X-Forwarded-For": "3.3.3.3"}, "2.2.2.2"),
    ({"X-Forwarded-For": " 3.3.3.3 , 4.4.4.4"}, "3.3.3.3"),
    ({}, "10.0.0.9"),
])
def test_client_ip_prefers_proxy_headers(headers, expected):
    assert client_ip(_request(headers)) == expected


def test_client_ip_without_any_source_is_none():
    assert client_ip(_request({}, client=None)) is None
