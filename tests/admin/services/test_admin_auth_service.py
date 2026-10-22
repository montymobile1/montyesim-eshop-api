from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gotrue.errors import AuthApiError, AuthRetryableError

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN, permissions_for
from app.exceptions.admin import AdminForbidden, AdminRateLimited, AdminUnauthorized, AdminUpstreamError
from app.services.admin.auth_service import AdminAuthService, LoginRateLimiter, _parse_rate_limit
from app.repo.admin.login_attempt_repo import LoginAttemptRepo
from tests.admin.conftest import FakeClock, FakeLoginAttemptStore, make_admin, make_auth_user, make_session

WRONG = AuthApiError("Invalid login credentials", 400, "invalid_credentials")


@pytest.fixture
def client():
    return MagicMock(name="supabase-client")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(clock):
    return FakeLoginAttemptStore(clock)


def _limiter(store):
    return LoginRateLimiter(max_failures=5, window_seconds=300, store_factory=lambda: store)


@pytest.fixture
def limiter(store):
    return _limiter(store)


@pytest.fixture
def service(client, limiter):
    return AdminAuthService(client_factory=lambda: client, rate_limiter=limiter)


def _fail(service, email="admin@example.com", ip="1.2.3.4", times=1):
    for _ in range(times):
        with pytest.raises(AdminUnauthorized):
            service.login(email, "wrong", ip)


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
def test_supabase_auth_outage_is_502_not_a_failed_attempt(service, client, store, error):
    client.auth.sign_in_with_password.side_effect = error

    with pytest.raises(AdminUpstreamError) as err:
        service.login("admin@example.com", "secret", "1.2.3.4")

    assert err.value.code == 502
    assert store.rows == {}
    assert store.finish_calls == [(1, "aborted")]


def test_unexpected_supabase_error_is_not_counted_as_a_failed_attempt(service, client, store):
    client.auth.sign_in_with_password.side_effect = RuntimeError("socket closed")

    with pytest.raises(RuntimeError):
        service.login("admin@example.com", "secret", "1.2.3.4")

    assert store.rows == {}


# ---------- rate limit (TEOS-109: 5 failures per 300 s per IP and per email, 6th -> 429) ----------

def test_sixth_attempt_from_the_same_ip_and_email_is_429_without_calling_supabase(service, client):
    client.auth.sign_in_with_password.side_effect = WRONG
    _fail(service, times=5)

    with pytest.raises(AdminRateLimited) as err:
        service.login("admin@example.com", "right", "1.2.3.4")

    assert err.value.code == 429
    assert err.value.details == "ADMIN_TOO_MANY_ATTEMPTS"
    assert client.auth.sign_in_with_password.call_count == 5
    # and every attempt after it, while the window lasts
    for _ in range(3):
        with pytest.raises(AdminRateLimited):
            service.login("admin@example.com", "right", "1.2.3.4")
    assert client.auth.sign_in_with_password.call_count == 5


def test_per_ip_limit_is_shared_by_workers(client, store):
    """Two app instances (workers) with their own limiter and service, sharing the store."""
    worker_a = AdminAuthService(client_factory=lambda: client, rate_limiter=_limiter(store))
    worker_b = AdminAuthService(client_factory=lambda: client, rate_limiter=_limiter(store))
    client.auth.sign_in_with_password.side_effect = WRONG
    for i, worker in enumerate([worker_a, worker_b, worker_a, worker_b, worker_a]):
        _fail(worker, email=f"user{i}@example.com")

    for worker in (worker_a, worker_b):
        with pytest.raises(AdminRateLimited):
            worker.login("someone-else@example.com", "x", "1.2.3.4")
    assert client.auth.sign_in_with_password.call_count == 5


def test_per_email_limit_is_shared_by_workers_and_ips(client, store):
    worker_a = AdminAuthService(client_factory=lambda: client, rate_limiter=_limiter(store))
    worker_b = AdminAuthService(client_factory=lambda: client, rate_limiter=_limiter(store))
    client.auth.sign_in_with_password.side_effect = WRONG
    for i in range(5):
        _fail(worker_a if i % 2 else worker_b, email="admin@example.com", ip=f"10.0.0.{i}")

    # same email (any case, spaces around it) from a new IP, on either worker
    for worker in (worker_a, worker_b):
        with pytest.raises(AdminRateLimited):
            worker.login("  ADMIN@Example.com ", "right", "9.9.9.9")


def test_second_email_from_a_limited_ip_is_still_limited_by_the_ip_rule(service, client, store):
    client.auth.sign_in_with_password.side_effect = WRONG
    _fail(service, email="first@example.com", times=5)

    with pytest.raises(AdminRateLimited):
        service.login("second@example.com", "x", "1.2.3.4")
    # the second email itself is not limited from another IP
    _fail(service, email="second@example.com", ip="5.6.7.8")


def test_different_emails_from_one_ip_hit_the_limit_on_the_sixth(service, client):
    client.auth.sign_in_with_password.side_effect = WRONG
    for i in range(5):
        _fail(service, email=f"user{i}@example.com")

    with pytest.raises(AdminRateLimited):
        service.login("user5@example.com", "x", "1.2.3.4")


def test_attempts_are_allowed_again_after_the_window(service, client, clock):
    client.auth.sign_in_with_password.side_effect = WRONG
    _fail(service, times=5)
    clock.now += 299
    with pytest.raises(AdminRateLimited):
        service.login("admin@example.com", "x", "1.2.3.4")

    clock.now += 2  # the first failure is now older than 300 s
    client.auth.sign_in_with_password.side_effect = None
    client.auth.sign_in_with_password.return_value = _auth_response()
    assert service.login("admin@example.com", "right", "1.2.3.4").user.role == ROLE_ADMIN


def test_valid_login_under_the_limit_succeeds(service, client):
    client.auth.sign_in_with_password.side_effect = [WRONG] * 4 + [_auth_response()]
    _fail(service, times=4)

    assert service.login("admin@example.com", "right", "1.2.3.4").user.role == ROLE_ADMIN


def test_successful_login_clears_only_that_ip_and_email_pair(service, client, store):
    client.auth.sign_in_with_password.side_effect = [WRONG, WRONG, WRONG, _auth_response()]
    _fail(service, email="admin@example.com", times=2)
    _fail(service, email="other@example.com", times=1)
    service.login("Admin@example.com", "right", "1.2.3.4")

    assert list(store.rows.values()) == [("1.2.3.4", "other@example.com", 1000.0)]


def test_a_refused_attempt_does_not_extend_the_lockout(service, client, store):
    client.auth.sign_in_with_password.side_effect = WRONG
    _fail(service, times=5)
    for _ in range(10):
        with pytest.raises(AdminRateLimited):
            service.login("admin@example.com", "x", "1.2.3.4")

    assert len(store.rows) == 5


def test_limiter_sends_normalised_keys_and_the_configured_limit(service, client, store):
    client.auth.sign_in_with_password.side_effect = WRONG
    _fail(service, email="  Admin@Example.COM ", ip=None)

    assert store.begin_calls == [("-", "admin@example.com", 5, 300)]


def test_store_unavailable_fails_closed_with_502_without_calling_supabase(client):
    broken = MagicMock(name="store")
    broken.begin.side_effect = AdminUpstreamError("DATABASE_ERROR")
    service = AdminAuthService(client_factory=lambda: client,
                               rate_limiter=LoginRateLimiter(5, 300, store_factory=lambda: broken))

    with pytest.raises(AdminUpstreamError) as err:
        service.login("admin@example.com", "right", "1.2.3.4")

    assert err.value.code == 502
    assert err.value.details == "RATE_LIMIT_STORE_UNAVAILABLE"
    client.auth.sign_in_with_password.assert_not_called()


def test_store_answer_without_attempt_id_fails_closed(client):
    odd = MagicMock(name="store")
    odd.begin.return_value = (True, None)
    service = AdminAuthService(client_factory=lambda: client,
                               rate_limiter=LoginRateLimiter(5, 300, store_factory=lambda: odd))

    with pytest.raises(AdminUpstreamError):
        service.login("admin@example.com", "right", "1.2.3.4")
    client.auth.sign_in_with_password.assert_not_called()


def test_successful_login_survives_a_failed_reset(client):
    store = MagicMock(name="store")
    store.begin.return_value = (True, 7)
    store.finish.side_effect = RuntimeError("network")
    client.auth.sign_in_with_password.return_value = _auth_response()
    service = AdminAuthService(client_factory=lambda: client,
                               rate_limiter=LoginRateLimiter(5, 300, store_factory=lambda: store))

    assert service.login("admin@example.com", "right", "1.2.3.4").user.role == ROLE_ADMIN
    store.finish.assert_called_once_with(7, "success")


def test_limiter_creates_its_store_lazily():
    factory = MagicMock(name="factory")
    LoginRateLimiter(5, 300, store_factory=factory)

    factory.assert_not_called()


# ---------- LoginAttemptRepo (the RPC calls) ----------

@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setattr("app.repo.base_repo.supabase_client", lambda: MagicMock(name="db"))
    return LoginAttemptRepo()


@pytest.mark.parametrize("result, expected", [
    ({"allowed": True, "attempt_id": 12}, (True, 12)),
    ([{"allowed": False, "attempt_id": None}], (False, None)),
])
def test_repo_begin_calls_the_rpc_and_reads_the_decision(repo, monkeypatch, result, expected):
    calls = []
    monkeypatch.setattr(repo, "rpc", lambda name, params: calls.append((name, params)) or result)

    assert repo.begin("1.2.3.4", "admin@example.com", 5, 300) == expected
    assert calls == [("admin_login_attempt_begin",
                      {"p_ip": "1.2.3.4", "p_email": "admin@example.com", "p_limit": 5, "p_window_seconds": 300})]


def test_repo_begin_without_a_decision_raises(repo, monkeypatch):
    monkeypatch.setattr(repo, "rpc", lambda name, params: None)

    with pytest.raises(ValueError):
        repo.begin("1.2.3.4", "admin@example.com", 5, 300)


def test_repo_finish_calls_the_rpc(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "rpc", lambda name, params: calls.append((name, params)))

    repo.finish(12, "success")

    assert calls == [("admin_login_attempt_finish", {"p_attempt_id": 12, "p_outcome": "success"})]


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
