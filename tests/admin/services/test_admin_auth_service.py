from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gotrue.errors import AuthApiError, AuthRetryableError

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN, permissions_for
from app.exceptions.admin import AdminForbidden, AdminRateLimited, AdminUnauthorized, AdminUpstreamError
from app.services.admin.auth_service import AdminAuthService, LoginRateLimiter, _parse_rate_limit
from tests.admin.conftest import make_admin, make_auth_user, make_session


@pytest.fixture
def client():
    return MagicMock(name="supabase-client")


@pytest.fixture
def limiter():
    return LoginRateLimiter(max_failures=3, window_seconds=60, clock=lambda: 1000.0)


@pytest.fixture
def service(client, limiter):
    return AdminAuthService(client_factory=lambda: client, rate_limiter=limiter)


def _auth_response(role=ROLE_ADMIN):
    return SimpleNamespace(session=make_session(), user=make_auth_user(role))


# ---------- login ----------

def test_login_returns_session_with_role_and_sorted_permissions(service, client):
    client.auth.sign_in_with_password.return_value = _auth_response(ROLE_SUPER_ADMIN)

    session = service.login(" Admin@Example.com ", "secret", "1.2.3.4")

    client.auth.sign_in_with_password.assert_called_once_with({"email": "Admin@Example.com", "password": "secret"})
    assert session.access_token == "access-token"
    assert session.refresh_token == "refresh-token"
    assert session.expires_in == 3600
    assert session.user.role == ROLE_SUPER_ADMIN
    assert session.user.permissions == sorted(permissions_for(ROLE_SUPER_ADMIN))
    client.auth.admin.sign_out.assert_not_called()


def test_login_with_wrong_password_is_401_login_failed(service, client):
    client.auth.sign_in_with_password.side_effect = AuthApiError("Invalid login credentials", 400, "invalid_credentials")

    with pytest.raises(AdminUnauthorized) as err:
        service.login("admin@example.com", "wrong", "1.2.3.4")

    assert err.value.code == 401
    assert err.value.details == "ADMIN_LOGIN_FAILED"


def test_account_without_admin_role_gets_403_and_its_session_is_revoked(service, client):
    client.auth.sign_in_with_password.return_value = _auth_response(role=None)

    with pytest.raises(AdminForbidden) as err:
        service.login("customer@example.com", "secret", "1.2.3.4")

    assert err.value.details == "ADMIN_ROLE_REQUIRED"
    client.auth.admin.sign_out.assert_called_once_with("access-token", "local")


def test_403_is_returned_even_if_revoking_the_session_fails(service, client):
    client.auth.sign_in_with_password.return_value = _auth_response(role="user")
    client.auth.admin.sign_out.side_effect = RuntimeError("network")

    with pytest.raises(AdminForbidden):
        service.login("customer@example.com", "secret", None)


@pytest.mark.parametrize("error", [
    AuthApiError("boom", 503, "unexpected_failure"),
    AuthRetryableError("timeout", 0),
])
def test_supabase_auth_outage_is_502_not_a_failed_attempt(service, client, limiter, error):
    client.auth.sign_in_with_password.side_effect = error

    with pytest.raises(AdminUpstreamError) as err:
        service.login("admin@example.com", "secret", "1.2.3.4")

    assert err.value.code == 502
    assert limiter._failures == {}


def test_repeated_failures_lock_out_that_ip_and_email_without_calling_supabase(service, client):
    client.auth.sign_in_with_password.side_effect = AuthApiError("Invalid login credentials", 400, "invalid_credentials")
    for _ in range(3):
        with pytest.raises(AdminUnauthorized):
            service.login("admin@example.com", "wrong", "1.2.3.4")

    with pytest.raises(AdminRateLimited) as err:
        service.login("ADMIN@example.com", "right", "1.2.3.4")

    assert err.value.code == 429
    assert client.auth.sign_in_with_password.call_count == 3
    # another IP for the same email is counted separately
    client.auth.sign_in_with_password.side_effect = None
    client.auth.sign_in_with_password.return_value = _auth_response()
    assert service.login("admin@example.com", "right", "5.6.7.8").user.role == ROLE_ADMIN


def test_successful_login_clears_previous_failures(service, client, limiter):
    client.auth.sign_in_with_password.side_effect = [
        AuthApiError("Invalid login credentials", 400, "invalid_credentials"),
        _auth_response(),
    ]
    with pytest.raises(AdminUnauthorized):
        service.login("admin@example.com", "wrong", "1.2.3.4")
    service.login("admin@example.com", "right", "1.2.3.4")

    assert limiter._failures == {}


# ---------- rate limiter ----------

def test_failures_older_than_the_window_are_forgotten():
    now = [0.0]
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, clock=lambda: now[0])
    limiter.record_failure("k")
    limiter.record_failure("k")
    with pytest.raises(AdminRateLimited):
        limiter.check("k")

    now[0] = 60.0
    limiter.check("k")
    assert "k" not in limiter._failures


@pytest.mark.parametrize("value, expected", [
    ("10/60", (10, 60)),
    (None, (5, 300)),
    ("abc", (5, 300)),
    ("0/60", (5, 300)),
    ("5", (5, 300)),
])
def test_rate_limit_setting_is_parsed_with_a_safe_default(value, expected):
    assert _parse_rate_limit(value) == expected


# ---------- refresh ----------

def test_refresh_returns_a_new_session(service, client):
    client.auth.refresh_session.return_value = _auth_response()

    session = service.refresh("refresh-token")

    client.auth.refresh_session.assert_called_once_with("refresh-token")
    assert session.user.role == ROLE_ADMIN


def test_refresh_with_invalid_token_is_401(service, client):
    client.auth.refresh_session.side_effect = AuthApiError("Invalid Refresh Token", 400, "refresh_token_not_found")

    with pytest.raises(AdminUnauthorized) as err:
        service.refresh("bad")

    assert err.value.details == "INVALID_REFRESH_TOKEN"


def test_refresh_after_the_role_was_removed_is_403(service, client):
    client.auth.refresh_session.return_value = _auth_response(role=None)

    with pytest.raises(AdminForbidden):
        service.refresh("refresh-token")

    client.auth.admin.sign_out.assert_called_once_with("access-token", "local")


# ---------- logout / me ----------

def test_logout_ends_only_the_current_session(service, client):
    service.logout(make_admin(token="the-token"))

    client.auth.admin.sign_out.assert_called_once_with("the-token", "local")


def test_logout_of_an_already_ended_session_succeeds(service, client):
    client.auth.admin.sign_out.side_effect = AuthApiError("session not found", 404, "session_not_found")

    service.logout(make_admin())


def test_logout_during_an_outage_is_502(service, client):
    client.auth.admin.sign_out.side_effect = AuthApiError("boom", 500, "unexpected_failure")

    with pytest.raises(AdminUpstreamError):
        service.logout(make_admin())


def test_me_returns_the_admin_profile():
    profile = AdminAuthService.me(make_admin(ROLE_ADMIN))

    assert profile.role == ROLE_ADMIN
    assert profile.permissions == sorted(permissions_for(ROLE_ADMIN))
