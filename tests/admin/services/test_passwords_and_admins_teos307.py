"""TEOS-307: own password change, password reset from the Admins page, revoke admin access (services)."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from gotrue.errors import AuthApiError, AuthRetryableError

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN, SUPER_ONLY, permissions_for
from app.dependencies.admin_security import is_email_link_token, resolve_admin
from app.exceptions.admin import (AdminConflict, AdminError, AdminForbidden, AdminNotFound, AdminRateLimited,
                                  AdminServiceUnavailable, AdminUnauthorized, AdminUpstreamError,
                                  AdminValidationError)
from app.repo.admin.base import AuditContext
from app.schemas.admin.auth import check_password_rules
from app.schemas.admin.common import PageParams
from app.services.admin.admin_account_service import AdminAccountService
from app.services.admin.auth_service import AdminAuthService, LoginRateLimiter
from tests.admin.conftest import FakeClock, FakeLoginAttemptStore, make_admin, make_auth_user, make_session
from tests.admin.services.test_trusted_devices_signin import FakeDevices

ADMIN_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
GOOD = "N3w-Passw0rd!x"
WRONG = AuthApiError("Invalid login credentials", 400, "invalid_credentials")


def token(*methods):
    return jwt.encode({"sub": ADMIN_ID, "session_id": "sess-1", "amr": [{"method": m, "timestamp": 1} for m in methods]},
                      "k", algorithm="HS256")


class FakeAudit:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def log(self, ctx, table, operation, **kwargs):
        if self.fail:
            raise RuntimeError("audit down")
        self.rows.append((ctx, table, operation, kwargs))


@pytest.fixture
def client():
    return MagicMock(name="supabase-client")


@pytest.fixture
def store():
    return FakeLoginAttemptStore(FakeClock())


@pytest.fixture
def audit():
    return FakeAudit()


@pytest.fixture
def revoked():
    return []


@pytest.fixture
def devices():
    return FakeDevices()


@pytest.fixture
def auth(client, store, audit, revoked, devices):
    limiter = LoginRateLimiter(max_failures=5, window_seconds=300, store_factory=lambda: store)
    return AdminAuthService(client_factory=lambda: client, rate_limiter=limiter,
                            session_revoker=lambda uid: revoked.append(uid) or {"sessions": 1, "refresh_tokens": 1},
                            audit_factory=lambda: audit, device_service_factory=lambda: devices)


CTX = AuditContext(actor_id=ADMIN_ID, ip="1.2.3.4")


# ---------- password rules ----------

@pytest.mark.parametrize("value", ["Short1!a", "nouppercase1!xx", "NOLOWERCASE1!XX", "NoDigitsHere!!x",
                                   "NoSymbols12345x", " Leading1!space", "Trailing1!space ", "A1!" + "a" * 70])
def test_password_rules_refuse(value):
    with pytest.raises(ValueError):
        check_password_rules(value)


@pytest.mark.parametrize("value", [GOOD, "Ünïcode-Pass1x", "With spaces 1A!"])
def test_password_rules_accept(value):
    assert check_password_rules(value) == value


def test_security_manage_is_super_admin_only():
    assert "admin.security.manage" in SUPER_ONLY
    assert "admin.security.manage" not in permissions_for(ROLE_ADMIN)
    assert "admin.security.manage" in permissions_for(ROLE_SUPER_ADMIN)


# ---------- change password ----------

def _signed_in(client, user_id=ADMIN_ID):
    client.auth.sign_in_with_password.return_value = SimpleNamespace(
        session=make_session(access_token="new-access", refresh_token="new-refresh"),
        user=make_auth_user(ROLE_ADMIN, user_id=user_id))


def test_change_password_sets_it_signs_out_others_and_returns_the_new_session(auth, client, audit):
    _signed_in(client)

    result = auth.change_password(make_admin(), "Old-Passw0rd!x", GOOD, "1.2.3.4", CTX)

    client.auth.sign_in_with_password.assert_called_once_with({"email": "admin@example.com",
                                                               "password": "Old-Passw0rd!x"})
    client.auth.admin.update_user_by_id.assert_called_once_with(ADMIN_ID, {"password": GOOD})
    client.auth.admin.sign_out.assert_called_once_with("new-access", "others")
    assert result.access_token == "new-access" and result.refresh_token == "new-refresh"
    assert result.other_sessions_signed_out is True
    (ctx, table, operation, details), = audit.rows
    assert (table, operation, details["action"]) == ("auth.users", "UPDATE", "password_change")
    assert GOOD not in str(details) and "Old-Passw0rd!x" not in str(details)


def test_change_password_wrong_current_is_400_and_counts_as_a_failed_login(auth, client, store):
    client.auth.sign_in_with_password.side_effect = WRONG

    with pytest.raises(AdminError) as err:
        auth.change_password(make_admin(), "wrong", GOOD, "1.2.3.4", CTX)

    assert err.value.code == 400 and err.value.details == "current_password_invalid"
    assert len(store.rows) == 1
    client.auth.admin.update_user_by_id.assert_not_called()


def test_change_password_is_rate_limited_like_the_login(auth, client):
    client.auth.sign_in_with_password.side_effect = WRONG
    for _ in range(5):
        with pytest.raises(AdminError):
            auth.change_password(make_admin(), "wrong", GOOD, "1.2.3.4", CTX)

    with pytest.raises(AdminRateLimited):
        auth.change_password(make_admin(), "wrong", GOOD, "1.2.3.4", CTX)


def test_change_password_same_as_current_is_422_before_any_call(auth, client):
    with pytest.raises(AdminValidationError) as err:
        auth.change_password(make_admin(), GOOD, GOOD, "1.2.3.4", CTX)

    assert err.value.details == "same_password"
    client.auth.sign_in_with_password.assert_not_called()


@pytest.mark.parametrize("code, status, expected", [("same_password", 422, AdminValidationError),
                                                    ("weak_password", 422, AdminValidationError),
                                                    (None, 500, AdminUpstreamError)])
def test_change_password_refused_by_supabase_revokes_the_check_session(auth, client, code, status, expected):
    _signed_in(client)
    client.auth.admin.update_user_by_id.side_effect = AuthApiError("refused", status, code)

    with pytest.raises(expected):
        auth.change_password(make_admin(), "Old-Passw0rd!x", GOOD, "1.2.3.4", CTX)

    client.auth.admin.sign_out.assert_called_once_with("new-access", "local")


def test_change_password_session_of_another_account_is_refused(auth, client):
    _signed_in(client, user_id=OTHER_ID)

    with pytest.raises(AdminError) as err:
        auth.change_password(make_admin(), "Old-Passw0rd!x", GOOD, "1.2.3.4", CTX)

    assert err.value.details == "current_password_invalid"
    client.auth.admin.update_user_by_id.assert_not_called()


def test_change_password_reports_when_the_other_sessions_stay(auth, client, audit):
    _signed_in(client)
    client.auth.admin.sign_out.side_effect = AuthRetryableError("down", 503)

    result = auth.change_password(make_admin(), "Old-Passw0rd!x", GOOD, "1.2.3.4", CTX)

    assert result.other_sessions_signed_out is False
    assert audit.rows[0][3]["other_sessions_signed_out"] is False


def test_change_password_audit_failure_does_not_fail_the_change(client, store):
    _signed_in(client)
    service = AdminAuthService(client_factory=lambda: client,
                               rate_limiter=LoginRateLimiter(5, 300, store_factory=lambda: store),
                               audit_factory=lambda: FakeAudit(fail=True), device_service_factory=FakeDevices)

    assert service.change_password(make_admin(), "Old-Passw0rd!x", GOOD, "1.2.3.4", CTX).access_token == "new-access"


# ---------- reset password (link page) ----------

def test_email_link_tokens():
    assert is_email_link_token(token("recovery"))
    assert is_email_link_token(token("otp"))
    assert not is_email_link_token(token("password"))
    assert not is_email_link_token(token("password", "recovery"))
    assert not is_email_link_token("not-a-jwt")


def _recovery(role=ROLE_ADMIN):
    """Supabase's verify_otp answer for a valid recovery token: the link's session and the account."""
    return SimpleNamespace(session=SimpleNamespace(access_token="recovery-access"), user=make_auth_user(role))


def test_reset_password_verifies_the_token_sets_it_and_signs_out_every_session(auth, client, audit, revoked):
    client.auth.verify_otp.return_value = _recovery()

    result = auth.complete_password_reset("hashed-recovery-token", GOOD, "1.2.3.4")

    client.auth.verify_otp.assert_called_once_with({"token_hash": "hashed-recovery-token", "type": "recovery"})
    client.auth.admin.update_user_by_id.assert_called_once_with(ADMIN_ID, {"password": GOOD})
    assert revoked == [ADMIN_ID] and result.sessions_signed_out is True
    ctx, _, _, details = audit.rows[0]
    assert ctx.actor_id == ADMIN_ID and details["action"] == "password_reset"


@pytest.mark.parametrize("value", ["", None])
def test_reset_password_without_a_token_is_400_before_supabase(auth, client, value):
    with pytest.raises(AdminError) as err:
        auth.complete_password_reset(value, GOOD, None)

    assert err.value.code == 400 and err.value.details == "reset_link_invalid"
    client.auth.verify_otp.assert_not_called()


def test_reset_password_with_an_expired_or_used_link_is_400(auth, client):
    client.auth.verify_otp.side_effect = AuthApiError("Token has expired or is invalid", 403, "otp_expired")

    with pytest.raises(AdminError) as err:
        auth.complete_password_reset("used-token", GOOD, None)

    assert err.value.details == "reset_link_invalid"
    client.auth.admin.update_user_by_id.assert_not_called()


def test_reset_password_when_supabase_is_down_is_502(auth, client):
    client.auth.verify_otp.side_effect = AuthRetryableError("down", 0)

    with pytest.raises(AdminUpstreamError) as err:
        auth.complete_password_reset("t", GOOD, None)

    assert err.value.details == "AUTH_SERVICE_UNAVAILABLE"


def test_reset_password_of_an_account_without_admin_role_is_403_and_signed_out(auth, client):
    client.auth.verify_otp.return_value = _recovery(role=None)

    with pytest.raises(AdminForbidden):
        auth.complete_password_reset("t", GOOD, None)

    client.auth.admin.update_user_by_id.assert_not_called()
    client.auth.admin.sign_out.assert_called_once_with("recovery-access", "local")


def test_reset_password_refused_by_supabase_signs_the_link_session_out(auth, client):
    client.auth.verify_otp.return_value = _recovery()
    client.auth.admin.update_user_by_id.side_effect = AuthApiError("weak", 422, "weak_password")

    with pytest.raises(AdminValidationError):
        auth.complete_password_reset("t", GOOD, None)

    client.auth.admin.sign_out.assert_called_once_with("recovery-access", "local")


def test_email_link_session_is_refused_on_admin_routes(supabase_auth, monkeypatch):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_SUPER_ADMIN))
    devices = FakeDevices()
    devices.add(session_id="sess-1")  # TEOS-255: even a session with a device row
    monkeypatch.setattr("app.dependencies.admin_security.session_device_lookup", devices.session_device)

    with pytest.raises(AdminUnauthorized) as err:
        resolve_admin(token("recovery"))

    assert err.value.details == "EMAIL_LINK_SESSION_NOT_ALLOWED"
    assert resolve_admin(token("password")).role == ROLE_SUPER_ADMIN


def test_refresh_of_an_email_link_session_is_refused(auth, client):
    client.auth.refresh_session.return_value = SimpleNamespace(
        session=make_session(access_token=token("recovery")), user=make_auth_user(ROLE_ADMIN))

    with pytest.raises(AdminUnauthorized):
        auth.refresh("refresh-token")


# ---------- Admins page ----------

def _user(user_id, email, role, **extra):
    return SimpleNamespace(id=user_id, email=email, app_metadata={"provider": "email", "role": role} if role else {},
                           created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), last_sign_in_at=None, **extra)


RESET_TOKEN = "pkce_0a1b2c3d4e5f"
RESET_LINK = "https://admin.example.com/reset-password#token=pkce_0a1b2c3d4e5f"


@pytest.fixture
def mailed():
    """The reset emails sent through our SMTP: (recipient, link)."""
    return []


@pytest.fixture
def portal_sign_ins():
    """admin_devices.last_sign_in_at per admin (lower-case id), the Admins page "Last sign-in"."""
    return {}


@pytest.fixture
def accounts(client, audit, revoked, mailed, portal_sign_ins):
    client.auth.admin.generate_link.return_value = SimpleNamespace(
        properties=SimpleNamespace(hashed_token=RESET_TOKEN, verification_type="recovery",
                                   action_link="https://project.supabase.co/auth/v1/verify?token=x"))
    return AdminAccountService(client_factory=lambda: client, device_revoker=lambda *a, **k: 0, audit_factory=lambda: audit,
                               session_revoker=lambda uid: revoked.append(uid) or {"sessions": 1, "refresh_tokens": 0},
                               reset_url=lambda: "https://admin.example.com/reset-password",
                               reset_sender=lambda email, link: mailed.append((email, link)),
                               smtp_check=lambda: True,
                               portal_sign_ins=lambda ids: {i.lower(): portal_sign_ins[i.lower()]
                                                            for i in ids if i.lower() in portal_sign_ins})


def test_list_keeps_admins_only_super_admins_first_with_facets(accounts, client):
    client.auth.admin.list_users.return_value = [
        _user("c", "zed@x.com", ROLE_ADMIN), _user("a", "customer@x.com", None),
        _user(ADMIN_ID, "me@x.com", ROLE_SUPER_ADMIN), _user("b", "amy@x.com", ROLE_ADMIN),
        _user("d", "odd@x.com", "user")]

    page = accounts.list(None, "all", PageParams(page=1, page_size=20), ADMIN_ID)

    assert [row.email for row in page.data] == ["me@x.com", "amy@x.com", "zed@x.com"]
    assert page.total_count == 3 and page.facets == {"all": 3, "super_admin": 1, "admin": 2}
    assert [row.is_current_user for row in page.data] == [True, False, False]
    client.auth.admin.list_users.assert_called_once_with(page=1, per_page=1000)


def test_last_sign_in_is_the_admin_portal_one_not_supabases(accounts, client, portal_sign_ins):
    """Supabase's last_sign_in_at is account-wide (a user portal sign-in moves it): never shown."""
    portal = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)
    user_portal_only = _user("b", "amy@x.com", ROLE_ADMIN)
    user_portal_only.last_sign_in_at = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    signed_in = _user("C-UPPER", "zed@x.com", ROLE_ADMIN)
    signed_in.last_sign_in_at = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    client.auth.admin.list_users.return_value = [user_portal_only, signed_in]
    portal_sign_ins["c-upper"] = portal.isoformat()

    page = accounts.list(None, "all", PageParams(page=1, page_size=20), ADMIN_ID)

    by_email = {row.email: row.last_sign_in_at for row in page.data}
    assert by_email == {"amy@x.com": None, "zed@x.com": portal}


def test_last_sign_ins_are_read_only_for_the_page_shown(client):
    asked = []
    client.auth.admin.list_users.return_value = [_user(str(i), f"ops{i}@x.com", ROLE_ADMIN) for i in range(5)]
    service = AdminAccountService(client_factory=lambda: client, reset_url=lambda: None, smtp_check=lambda: False,
                                  portal_sign_ins=lambda ids: asked.append(list(ids)) or {})

    service.list(None, "all", PageParams(page=2, page_size=2), ADMIN_ID)

    assert asked == [["2", "3"]]


def test_last_sign_ins_lookup_failure_is_502(client):
    client.auth.admin.list_users.return_value = [_user("b", "amy@x.com", ROLE_ADMIN)]

    def down(ids):
        raise RuntimeError("db down")

    service = AdminAccountService(client_factory=lambda: client, reset_url=lambda: None, smtp_check=lambda: False,
                                  portal_sign_ins=down)

    with pytest.raises(AdminUpstreamError) as err:
        service.list(None, "all", PageParams(page=1, page_size=20), ADMIN_ID)
    assert err.value.details == "SIGN_INS_LOOKUP_FAILED"


def test_list_search_role_tab_and_paging(accounts, client):
    client.auth.admin.list_users.return_value = [_user(str(i), f"ops{i}@x.com", ROLE_ADMIN) for i in range(5)] + [
        _user("s", "ops-super@x.com", ROLE_SUPER_ADMIN), _user("o", "other@x.com", ROLE_ADMIN)]

    page = accounts.list(" OPS ", "admin", PageParams(page=2, page_size=2), ADMIN_ID)

    assert page.facets == {"all": 6, "super_admin": 1, "admin": 5}
    assert page.total_count == 5 and [row.email for row in page.data] == ["ops2@x.com", "ops3@x.com"]


def test_list_reads_every_page_of_accounts(accounts, client):
    full = [_user(str(i), f"u{i}@x.com", None) for i in range(1000)]
    client.auth.admin.list_users.side_effect = [full, [_user("x", "late@x.com", ROLE_ADMIN)]]

    page = accounts.list(None, "all", PageParams(), ADMIN_ID)

    assert [row.email for row in page.data] == ["late@x.com"]
    assert client.auth.admin.list_users.call_count == 2


def test_reset_link_is_generated_by_supabase_and_sent_by_our_smtp(accounts, client, audit, mailed):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_ADMIN))

    result = accounts.send_password_reset(OTHER_ID, CTX)

    client.auth.admin.generate_link.assert_called_once_with({"type": "recovery", "email": "amy@x.com"})
    client.auth.reset_password_for_email.assert_not_called()  # Supabase's sender is never used
    # straight to the portal page, the token in the fragment: no Supabase address in the email
    assert mailed == [("amy@x.com", RESET_LINK)]
    assert "supabase" not in mailed[0][1]
    assert result.email == "amy@x.com"
    assert audit.rows[0][3]["action"] == "password_reset_email_sent"
    client.auth.admin.update_user_by_id.assert_not_called()  # nothing changes until the admin uses the link


def test_reset_and_revoke_of_own_account_are_409(accounts, client):
    for call in (lambda: accounts.send_password_reset(ADMIN_ID, CTX), lambda: accounts.revoke_access(ADMIN_ID, CTX)):
        with pytest.raises(AdminConflict) as err:
            call()
        assert err.value.conflict_code == "cannot_target_self"
    client.auth.admin.get_user_by_id.assert_not_called()


@pytest.mark.parametrize("found", ["missing", "customer"])
def test_reset_and_revoke_of_a_non_admin_are_404(accounts, client, found):
    if found == "missing":
        client.auth.admin.get_user_by_id.side_effect = AuthApiError("User not found", 404, "user_not_found")
    else:
        client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "c@x.com", None))

    with pytest.raises(AdminNotFound):
        accounts.send_password_reset(OTHER_ID, CTX)
    with pytest.raises(AdminNotFound):
        accounts.revoke_access(OTHER_ID, CTX)


def test_reset_without_the_portal_url_is_502_and_sends_nothing(client, audit):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_ADMIN))
    service = AdminAccountService(client_factory=lambda: client, device_revoker=lambda *a, **k: 0, audit_factory=lambda: audit, reset_url=lambda: None,
                                  smtp_check=lambda: True)

    with pytest.raises(AdminUpstreamError) as err:
        service.send_password_reset(OTHER_ID, CTX)

    assert err.value.details == "PASSWORD_RESET_URL_NOT_SET"
    client.auth.admin.generate_link.assert_not_called()


def test_reset_without_smtp_is_503_unavailable_and_generates_nothing(client, audit, mailed):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_ADMIN))
    service = AdminAccountService(client_factory=lambda: client, device_revoker=lambda *a, **k: 0,
                                  audit_factory=lambda: audit, reset_url=lambda: "https://admin.example.com/reset-password",
                                  reset_sender=lambda email, link: mailed.append((email, link)),
                                  smtp_check=lambda: False)

    with pytest.raises(AdminServiceUnavailable) as err:
        service.send_password_reset(OTHER_ID, CTX)

    assert err.value.code == 503 and json.loads(err.value.details)["code"] == "password_reset_unavailable"
    client.auth.admin.generate_link.assert_not_called()
    assert mailed == [] and audit.rows == []


@pytest.mark.parametrize("smtp, url, available", [(True, "https://a/reset-password", True), (False, "https://a/r", False),
                                                  (True, None, False)])
def test_list_says_whether_the_reset_link_is_available(client, smtp, url, available):
    client.auth.admin.list_users.return_value = [_user("b", "amy@x.com", ROLE_ADMIN)]
    service = AdminAccountService(client_factory=lambda: client, reset_url=lambda: url, smtp_check=lambda: smtp,
                                  portal_sign_ins=lambda ids: {})

    page = service.list(None, "all", PageParams(page=1, page_size=20), ADMIN_ID)

    assert page.password_reset_available is available


def test_reset_email_that_cannot_be_sent_is_502_and_not_audited(client, audit):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_ADMIN))
    client.auth.admin.generate_link.return_value = SimpleNamespace(properties=SimpleNamespace(hashed_token=RESET_TOKEN))

    def smtp_down(email, link):
        raise OSError("smtp down")

    service = AdminAccountService(client_factory=lambda: client, device_revoker=lambda *a, **k: 0,
                                  audit_factory=lambda: audit, reset_url=lambda: "https://admin.example.com/reset-password",
                                  reset_sender=smtp_down, smtp_check=lambda: True)

    with pytest.raises(AdminUpstreamError) as err:
        service.send_password_reset(OTHER_ID, CTX)

    assert err.value.details == "RESET_EMAIL_FAILED" and audit.rows == []


@pytest.mark.parametrize("exc, expected", [
    (AuthApiError("rate", 429, "over_email_send_rate_limit"), AdminRateLimited),
    (AuthApiError("Error sending recovery email", 500, "unexpected_failure"), AdminUpstreamError)])
def test_reset_email_errors(accounts, client, audit, exc, expected):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_ADMIN))
    client.auth.admin.generate_link.side_effect = exc

    with pytest.raises(expected):
        accounts.send_password_reset(OTHER_ID, CTX)
    assert audit.rows == []


def test_revoke_removes_the_role_signs_out_and_audits(accounts, client, audit, revoked):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_SUPER_ADMIN))
    ctx = AuditContext(actor_id=ADMIN_ID, ip="1.2.3.4", reason="left the company")

    result = accounts.revoke_access(OTHER_ID, ctx)

    client.auth.admin.update_user_by_id.assert_called_once_with(OTHER_ID, {"app_metadata": {"role": None}})
    assert revoked == [OTHER_ID]
    assert result.previous_role == ROLE_SUPER_ADMIN and result.sessions_signed_out is True
    row_ctx, _, _, details = audit.rows[0]
    assert row_ctx.reason == "left the company"
    assert details["old"] == {"role": ROLE_SUPER_ADMIN} and details["new"] == {"role": None}


def test_revoke_audit_failure_puts_the_role_back(client, revoked):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_ADMIN))
    service = AdminAccountService(client_factory=lambda: client, device_revoker=lambda *a, **k: 0, audit_factory=lambda: FakeAudit(fail=True),
                                  session_revoker=lambda uid: {"sessions": 0, "refresh_tokens": 0})

    with pytest.raises(AdminUpstreamError):
        service.revoke_access(OTHER_ID, CTX)

    assert client.auth.admin.update_user_by_id.call_args_list[-1].args == (OTHER_ID, {"app_metadata": {"role": ROLE_ADMIN}})


def test_revoke_when_sessions_cannot_be_deleted_still_removes_the_role(client, audit):
    client.auth.admin.get_user_by_id.return_value = SimpleNamespace(user=_user(OTHER_ID, "amy@x.com", ROLE_ADMIN))

    def fail(uid):
        raise RuntimeError("db down")

    service = AdminAccountService(client_factory=lambda: client, device_revoker=lambda *a, **k: 0, audit_factory=lambda: audit, session_revoker=fail)

    assert service.revoke_access(OTHER_ID, CTX).sessions_signed_out is False
