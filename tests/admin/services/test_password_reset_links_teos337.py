"""TEOS-337 / TEOS-339: the password reset links sent from the Admins page use the API's own token.

- TEOS-337: a pending link kept dying when the admin signed in (Supabase keeps one recovery token per user and the
  sign-in replaced it). Our token lives in ``admin_password_reset``: signing in never touches it.
- TEOS-339: a link older than 1 hour still worked (Supabase's own expiry, 24 h by default). Ours expires after
  1 hour, whatever the Supabase project's settings.

``FakeResetRepo`` (conftest) is an in-memory twin of ``AdminPasswordResetRepo`` (same conditions).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.exceptions.admin import AdminError, AdminForbidden, AdminRateLimited, AdminUpstreamError
from app.repo.admin.base import AuditContext
from app.services.admin.admin_account_service import AdminAccountService
from app.services.admin.auth_service import AdminAuthService, LoginRateLimiter
from app.services.admin.password_reset_tokens import (MAX_LINKS_PER_HOUR, RESET_LINK_TTL_SECONDS,
                                                      PasswordResetTokens, hash_token)
from tests.admin.conftest import FakeClock, FakeLoginAttemptStore, FakeResetRepo, make_auth_user

ADMIN_ID = "11111111-1111-1111-1111-111111111111"   # the super admin sending links
TARGET_ID = "22222222-2222-2222-2222-222222222222"  # the admin receiving them
GOOD = "N3w-Passw0rd!x"
PAGE = "https://admin.example.com/reset-password"
T0 = datetime(2026, 11, 16, 12, 24, 28, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeAudit:
    def __init__(self):
        self.rows = []

    def log(self, ctx, table, operation, **kwargs):
        self.rows.append((ctx, table, operation, kwargs))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(clock):
    return FakeResetRepo(clock)


@pytest.fixture
def tokens(repo, clock):
    return PasswordResetTokens(repo_factory=lambda: repo, clock=clock)


@pytest.fixture
def client():
    c = MagicMock(name="supabase-client")
    c.auth.admin.get_user_by_id.return_value = SimpleNamespace(
        user=make_auth_user(ROLE_ADMIN, user_id=TARGET_ID, email="amy@x.com"))
    return c


@pytest.fixture
def mailed():
    return []


@pytest.fixture
def audit():
    return FakeAudit()


@pytest.fixture
def devices():
    from tests.admin.services.test_trusted_devices_signin import FakeDevices
    return FakeDevices()


@pytest.fixture
def accounts(client, tokens, mailed, audit):
    return AdminAccountService(client_factory=lambda: client, audit_factory=lambda: audit,
                               device_revoker=lambda uid, ctx: 1, reset_url=lambda: PAGE,
                               reset_sender=lambda email, link: mailed.append((email, link)),
                               smtp_check=lambda: True, portal_sign_ins=lambda ids: {}, reset_tokens=tokens)


@pytest.fixture
def auth(client, tokens, audit, devices):
    limiter = LoginRateLimiter(max_failures=5, window_seconds=300,
                               store_factory=lambda: FakeLoginAttemptStore(FakeClock()))
    return AdminAuthService(client_factory=lambda: client, rate_limiter=limiter, audit_factory=lambda: audit,
                            device_service_factory=lambda: devices, reset_tokens=tokens)


CTX = AuditContext(actor_id=ADMIN_ID, ip="1.2.3.4")


def _send(accounts, mailed) -> str:
    """Sends a link to TARGET_ID and returns the token from the emailed link."""
    accounts.send_password_reset(TARGET_ID, CTX)
    link = mailed[-1][1]
    assert link.startswith(PAGE + "#token=")
    return link.split("#token=", 1)[1]


def _reset(auth, token, password=GOOD):
    return auth.complete_password_reset(token, password, "1.2.3.4")


def _invalid(call):
    with pytest.raises(AdminError) as err:
        call()
    assert err.value.code == 400 and err.value.details == "reset_link_invalid"


# ---------- TEOS-337: signing in does not kill the link ----------

def test_teos337_link_works_after_the_admin_signed_in_meanwhile(accounts, auth, client, mailed, clock):
    """QA: link sent 12:24:28, the admin signs in at 12:27:49, uses the link at 12:29:23: 200."""
    token = _send(accounts, mailed)
    clock.advance(201)
    # the admin's sign-in (password + email code) opens its session with a Supabase magic link: nothing of ours
    client.auth.admin.generate_link({"type": "magiclink", "email": "amy@x.com"})
    clock.advance(94)

    result = _reset(auth, token)

    assert result.sessions_signed_out is True
    client.auth.admin.update_user_by_id.assert_called_once_with(TARGET_ID, {"password": GOOD})


def test_the_link_no_longer_uses_supabase_recovery(accounts, auth, client, mailed):
    token = _send(accounts, mailed)
    _reset(auth, token)

    client.auth.verify_otp.assert_not_called()
    assert not any(c.args and c.args[0].get("type") == "recovery"
                   for c in client.auth.admin.generate_link.call_args_list)


# ---------- TEOS-339: 1 hour ----------

def test_teos339_link_older_than_1_hour_is_refused_and_nothing_changes(accounts, auth, client, mailed, clock):
    """QA: link sent 12:46:07, used 13:48 (62 minutes): 400 reset_link_invalid, password unchanged."""
    token = _send(accounts, mailed)
    clock.advance(62 * 60)

    _invalid(lambda: _reset(auth, token))
    client.auth.admin.update_user_by_id.assert_not_called()


def test_link_works_until_the_last_second_of_its_hour(accounts, auth, mailed, clock):
    token = _send(accounts, mailed)
    clock.advance(RESET_LINK_TTL_SECONDS - 1)

    assert _reset(auth, token).sessions_signed_out is True


def test_link_is_dead_exactly_at_1_hour(accounts, auth, mailed, clock):
    token = _send(accounts, mailed)
    clock.advance(RESET_LINK_TTL_SECONDS)

    _invalid(lambda: _reset(auth, token))


def test_the_audit_row_of_the_email_says_when_the_link_expires(accounts, mailed, audit):
    _send(accounts, mailed)

    details = audit.rows[-1][3]
    assert details["action"] == "password_reset_email_sent"
    assert details["expires_at"] == (T0 + timedelta(hours=1)).isoformat()
    assert "token" not in str(details).replace("expires_at", "")  # never the token


# ---------- single use, newest link only ----------

def test_link_works_once(accounts, auth, client, mailed):
    token = _send(accounts, mailed)
    _reset(auth, token)

    _invalid(lambda: _reset(auth, token))
    assert client.auth.admin.update_user_by_id.call_count == 1


def test_a_new_link_cancels_the_earlier_one(accounts, auth, mailed, clock):
    first = _send(accounts, mailed)
    clock.advance(60)
    second = _send(accounts, mailed)

    _invalid(lambda: _reset(auth, first))
    assert _reset(auth, second).sessions_signed_out is True


def test_email_that_fails_leaves_the_earlier_link_working(client, tokens, repo, audit, auth, clock):
    sent = []

    def sender(email, link):
        if sent:
            raise OSError("smtp down")
        sent.append(link)

    service = AdminAccountService(client_factory=lambda: client, audit_factory=lambda: audit,
                                  device_revoker=lambda uid, ctx: 1, reset_url=lambda: PAGE, reset_sender=sender,
                                  smtp_check=lambda: True, reset_tokens=tokens)
    service.send_password_reset(TARGET_ID, CTX)
    first = sent[0].split("#token=", 1)[1]

    with pytest.raises(AdminUpstreamError) as err:
        service.send_password_reset(TARGET_ID, CTX)
    assert err.value.details == "RESET_EMAIL_FAILED"

    unsent = [r for r in repo.rows.values() if r["token_hash"] != hash_token(first)]
    assert len(unsent) == 1 and unsent[0]["cancelled_at"] is not None  # the unsent link never works
    assert _reset(auth, first).sessions_signed_out is True


def test_a_new_password_from_the_link_cancels_other_pending_links(accounts, auth, repo, mailed):
    token = _send(accounts, mailed)
    other = {"admin_id": TARGET_ID, "token_hash": hash_token("other"), "created_by": ADMIN_ID,
             "expires_at": (T0 + timedelta(hours=1)).isoformat()}
    repo.create(other)  # e.g. a link whose email was still on its way

    _reset(auth, token)

    _invalid(lambda: _reset(auth, "other"))


def test_revoke_access_cancels_a_pending_link(accounts, auth, client, mailed):
    token = _send(accounts, mailed)

    accounts.revoke_access(TARGET_ID, CTX)

    _invalid(lambda: _reset(auth, token))


def test_changing_the_password_on_the_profile_page_cancels_a_pending_link(accounts, auth, client, mailed, devices):
    from app.config.admin_permissions import permissions_for
    from app.dependencies.admin_security import AdminUser
    from tests.admin.conftest import make_session
    token = _send(accounts, mailed)
    client.auth.sign_in_with_password.return_value = SimpleNamespace(
        session=make_session(), user=make_auth_user(ROLE_ADMIN, user_id=TARGET_ID, email="amy@x.com"))
    admin = AdminUser(id=TARGET_ID, email="amy@x.com", role=ROLE_ADMIN, permissions=permissions_for(ROLE_ADMIN),
                      token="access-token")

    auth.change_password(admin, "0ld-Passw0rd!x", GOOD, "1.2.3.4", CTX)

    _invalid(lambda: _reset(auth, token, "An0ther-Passw0rd!"))


# ---------- refusals ----------

def test_unknown_token_is_400(auth, client):
    _invalid(lambda: _reset(auth, "never-sent"))
    client.auth.admin.update_user_by_id.assert_not_called()


def test_supabase_refusing_the_password_gives_the_link_back(accounts, auth, client, mailed):
    """422 same/weak password: the admin picks another password with the same link."""
    from gotrue.errors import AuthApiError
    token = _send(accounts, mailed)
    client.auth.admin.update_user_by_id.side_effect = [AuthApiError("same", 422, "same_password"), None]

    with pytest.raises(AdminError) as err:
        _reset(auth, token)
    assert err.value.code == 422 and err.value.details == "same_password"

    assert _reset(auth, token, "An0ther-Passw0rd!").sessions_signed_out is True


def test_supabase_down_gives_the_link_back(accounts, auth, client, mailed):
    from gotrue.errors import AuthRetryableError
    token = _send(accounts, mailed)
    client.auth.admin.get_user_by_id.side_effect = [AuthRetryableError("down", 0),
                                                    client.auth.admin.get_user_by_id.return_value]

    with pytest.raises(AdminUpstreamError):
        _reset(auth, token)

    assert _reset(auth, token).sessions_signed_out is True


def test_account_no_longer_admin_is_403_and_the_link_is_used_up(accounts, auth, client, mailed):
    token = _send(accounts, mailed)
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=make_auth_user(None, user_id=TARGET_ID))

    with pytest.raises(AdminForbidden):
        _reset(auth, token)
    client.auth.admin.update_user_by_id.assert_not_called()
    _invalid(lambda: _reset(auth, token))


def test_deleted_account_is_400(accounts, auth, client, mailed):
    from gotrue.errors import AuthApiError
    token = _send(accounts, mailed)
    client.auth.admin.get_user_by_id.side_effect = AuthApiError("User not found", 404, "user_not_found")

    _invalid(lambda: _reset(auth, token))


def test_store_failure_on_reset_is_502(auth, repo):
    repo.fail = RuntimeError("db down")

    with pytest.raises(AdminUpstreamError) as err:
        _reset(auth, "t")
    assert err.value.details == "RESET_LINK_LOOKUP_FAILED"


def test_store_failure_on_send_is_502_and_nothing_is_emailed(accounts, repo, mailed):
    repo.fail = RuntimeError("db down")

    with pytest.raises(AdminUpstreamError) as err:
        accounts.send_password_reset(TARGET_ID, CTX)
    assert err.value.details == "RESET_EMAIL_FAILED" and mailed == []


def test_sixth_link_in_an_hour_is_429_without_email(accounts, mailed, clock):
    for _ in range(MAX_LINKS_PER_HOUR):
        _send(accounts, mailed)
        clock.advance(60)

    with pytest.raises(AdminRateLimited) as err:
        accounts.send_password_reset(TARGET_ID, CTX)
    assert err.value.details == "reset_email_rate_limited"
    assert len(mailed) == MAX_LINKS_PER_HOUR

    clock.advance(3600)
    _send(accounts, mailed)  # the hour moved on


# ---------- the token ----------

def test_token_is_random_long_and_only_its_hash_is_stored(accounts, repo, mailed):
    first = _send(accounts, mailed)
    second = _send(accounts, mailed)

    assert first != second and len(first) >= 43
    stored = [r["token_hash"] for r in repo.rows.values()]
    assert hash_token(first) in stored and first not in stored
    assert all(len(h) == 64 for h in stored)
    row = next(r for r in repo.rows.values() if r["token_hash"] == hash_token(first))
    assert row["admin_id"] == TARGET_ID and row["created_by"] == ADMIN_ID


# ---------- the repo (queries) ----------

def test_repo_claim_is_one_conditional_update(db_table):
    from app.repo.admin.password_reset_repo import AdminPasswordResetRepo
    chain = db_table.update.return_value.eq.return_value.is_.return_value.is_.return_value.gt.return_value
    chain.execute.return_value = SimpleNamespace(data=[{"id": "l1", "admin_id": TARGET_ID}])

    assert AdminPasswordResetRepo().claim("h" * 64, T0) == {"id": "l1", "admin_id": TARGET_ID}

    assert list(db_table.update.call_args.args[0]) == ["used_at"]
    db_table.update.return_value.eq.assert_called_once_with("token_hash", "h" * 64)
    assert db_table.update.return_value.eq.return_value.is_.call_args.args == ("used_at", "null")
    assert db_table.update.return_value.eq.return_value.is_.return_value.is_.call_args.args == ("cancelled_at", "null")
    gt =db_table.update.return_value.eq.return_value.is_.return_value.is_.return_value.gt
    assert gt.call_args.args == ("expires_at", T0.isoformat())


def test_repo_cancel_pending_keeps_the_new_link(db_table):
    from app.repo.admin.password_reset_repo import AdminPasswordResetRepo
    base = db_table.update.return_value.eq.return_value.is_.return_value.is_.return_value.gt.return_value
    base.neq.return_value.execute.return_value = SimpleNamespace(data=[{"id": "old"}])

    assert AdminPasswordResetRepo().cancel_pending(TARGET_ID, T0, except_id="new") == 1
    base.neq.assert_called_once_with("id", "new")


from tests.admin.services.test_trusted_devices_manage import db_table  # noqa: E402,F401  (fixture)
