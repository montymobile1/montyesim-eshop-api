"""HTTP contract of /api/v1/admin/auth/* and of the shared admin route behaviour."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from gotrue.errors import AuthApiError
from starlette.requests import Request

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN, permissions_for
from app.dependencies.admin_security import client_ip, rate_limit_ip, require_permission
from app.exceptions.admin import AdminForbidden, AdminUnauthorized
from app.schemas.admin.auth import AdminProfile, AdminSession
from app.services.admin.auth_service import AdminAuthService, LoginRateLimiter
from tests.admin.conftest import FakeClock, FakeLoginAttemptStore, make_admin, make_auth_user, make_session

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
    # the rate limit counts by the entry Render's proxy appended (rightmost), not the spoofable leftmost
    assert auth_service.calls == [("admin@example.com", "secret", "10.0.0.1")]


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


# ---------- rate_limit_ip (TEOS-109) ----------

@pytest.mark.parametrize("hops, headers, expected", [
    # default 1 hop (Render): the entry the proxy appended, i.e. the rightmost
    (None, {"X-Forwarded-For": "203.0.113.7"}, "203.0.113.7"),
    (None, {"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}, "203.0.113.7"),
    (None, {"X-Forwarded-For": " 1.1.1.1 ,2.2.2.2 , 203.0.113.7 "}, "203.0.113.7"),
    # client-sent headers that the old client_ip trusted are ignored
    (None, {"CF-Connecting-IP": "1.1.1.1", "X-Real-IP": "2.2.2.2", "X-Forwarded-For": "203.0.113.7"},
     "203.0.113.7"),
    (None, {"CF-Connecting-IP": "1.1.1.1", "X-Real-IP": "2.2.2.2"}, "10.0.0.9"),
    # two trusted hops (e.g. a CDN in front of Render)
    ("2", {"X-Forwarded-For": "6.6.6.6, 203.0.113.7, 172.16.0.1"}, "203.0.113.7"),
    ("2", {"X-Forwarded-For": "203.0.113.7"}, "10.0.0.9"),  # fewer entries than hops: TCP peer
    ("0", {"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}, "10.0.0.9"),  # 0 hops: TCP peer only
    ("junk", {"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}, "203.0.113.7"),  # invalid setting: default 1
    ("-1", {"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}, "203.0.113.7"),
    (None, {"X-Forwarded-For": "203.0.113.7, not-an-ip"}, "10.0.0.9"),  # not an IP: TCP peer
    (None, {"X-Forwarded-For": "2001:DB8::1"}, "2001:db8::1"),  # normalised
    (None, {}, "10.0.0.9"),
])
def test_rate_limit_ip_trusts_only_the_configured_proxy_hops(monkeypatch, hops, headers, expected):
    if hops is not None:
        monkeypatch.setenv("ADMIN_TRUSTED_PROXY_HOPS", hops)

    assert rate_limit_ip(_request(headers)) == expected


def test_rate_limit_ip_reads_every_x_forwarded_for_header():
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "client": ("10.0.0.9", 1),
                       "headers": [(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"203.0.113.7")]})

    assert rate_limit_ip(request) == "203.0.113.7"


def test_rate_limit_ip_without_any_source_is_none():
    assert rate_limit_ip(_request({}, client=None)) is None


# ---------- login rate limit over HTTP (real service, shared fake store) ----------

@pytest.fixture
def limited_login(monkeypatch):
    """The real AdminAuthService behind /login, with a fake Supabase and a fake shared store."""
    from app.api.v1.admin import auth as auth_routes
    supabase = MagicMock(name="supabase-client")
    store = FakeLoginAttemptStore(FakeClock())
    real = AdminAuthService(client_factory=lambda: supabase,
                            rate_limiter=LoginRateLimiter(5, 300, store_factory=lambda: store))
    monkeypatch.setattr(auth_routes, "service", real)
    supabase.auth.sign_in_with_password.side_effect = AuthApiError("Invalid login credentials", 400,
                                                                   "invalid_credentials")
    return SimpleNamespace(supabase=supabase, store=store)


def _login(api_client, email="admin@example.com", xff="203.0.113.7", password="wrong"):
    return api_client.post(f"{BASE}/login", json={"email": email, "password": password},
                           headers={"X-Forwarded-For": xff})


def test_spoofed_leftmost_forwarded_for_does_not_bypass_the_ip_limit(api_client, limited_login):
    # a new fake "client IP" and a new email on every request; Render appends the real one
    for i in range(5):
        _assert_failed(_login(api_client, email=f"user{i}@example.com", xff=f"6.6.6.{i}, 203.0.113.7"), 401)

    body = _assert_failed(_login(api_client, email="user5@example.com", xff="7.7.7.7, 203.0.113.7"), 429,
                          "ADMIN_TOO_MANY_ATTEMPTS")
    assert body["message"] == "Too many sign-in attempts. Please try again later."
    assert limited_login.supabase.auth.sign_in_with_password.call_count == 5
    assert {row[0] for row in limited_login.store.rows.values()} == {"203.0.113.7"}


def test_wrong_password_is_still_401_with_the_same_body(api_client, limited_login):
    body = _assert_failed(_login(api_client), 401, "ADMIN_LOGIN_FAILED")

    assert body["message"] == "Invalid email or password."


def test_valid_login_is_200_when_under_the_limit(api_client, limited_login):
    for _ in range(4):
        _assert_failed(_login(api_client), 401)
    limited_login.supabase.auth.sign_in_with_password.side_effect = None
    limited_login.supabase.auth.sign_in_with_password.return_value = SimpleNamespace(
        session=make_session(), user=make_auth_user(ROLE_ADMIN))

    response = _login(api_client, password="right")

    assert response.status_code == 200
    assert response.json()["data"]["user"]["role"] == ROLE_ADMIN
    assert limited_login.store.rows == {}
