"""UserAdminService, UserAdminRepo and UserWalletAdminRepo (MS-5079) with the database and GoTrue mocked."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from gotrue.errors import AuthApiError

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError
from app.repo.admin.base import AuditContext, Paged
from app.repo.admin.user_repo import UserAdminRepo, UserSearch, UserWalletAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.users import AdminUserBan, AdminUserUnban
from app.services.admin.user_service import (AUTH_USERS_TABLE, CANNOT_BAN_SELF, SESSION_REVOKE_FAILED,
                                             UNBAN_DURATION, SessionRevokeError, UserAdminService,
                                             remaining_ban_duration, revoke_user_sessions)

USER_ID = "33333333-3333-3333-3333-333333333333"
CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4", reason="fraud")
PAGE = PageParams(page=2, page_size=10)
FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()


def _row(**extra):
    return {"id": USER_ID, "email": "jane@example.com", "first_name": "Jane", "is_banned": False,
            "banned_until": None, **extra}


@pytest.fixture
def repo():
    return MagicMock(spec=UserAdminRepo)


@pytest.fixture
def wallets():
    return MagicMock(spec=UserWalletAdminRepo)


@pytest.fixture
def devices():
    return MagicMock()


@pytest.fixture
def audit():
    return MagicMock()


@pytest.fixture
def client():
    client = MagicMock(name="supabase-client")
    client.auth.admin.update_user_by_id.return_value = SimpleNamespace(
        user=SimpleNamespace(id=USER_ID, email="jane@example.com", banned_until=None))
    return client


@pytest.fixture
def revoker():
    return MagicMock(name="session-revoker")


@pytest.fixture
def svc(repo, wallets, devices, audit, client, revoker):
    return UserAdminService(repo=repo, wallets=wallets, devices=devices, audit=audit, client_factory=lambda: client,
                            session_revoker=revoker)


# ---------- reads ----------

def test_list_returns_rows_total_and_facets(svc, repo):
    repo.search.return_value = UserSearch(rows=[_row()], total=7, facets={"all": 7, "banned": 2, "active": 5})

    page = svc.list("ja", "banned", PAGE)

    repo.search.assert_called_once_with("ja", "banned", PAGE, sort="created_at", direction="desc")
    assert page.data == [_row()] and page.total_count == 7
    assert page.facets == {"all": 7, "banned": 2, "active": 5}


def test_list_passes_sort_and_direction(svc, repo):
    repo.search.return_value = UserSearch(rows=[], total=0, facets={})

    svc.list(None, "all", PAGE, sort="email", direction="asc")

    repo.search.assert_called_once_with(None, "all", PAGE, sort="email", direction="asc")


def test_get_adds_wallet(svc, repo, wallets):
    repo.get.return_value = _row()
    wallets.for_user.return_value = {"amount": 3.5, "currency": "EUR"}

    assert svc.get(USER_ID) == {**_row(), "wallet": {"amount": 3.5, "currency": "EUR"}}
    wallets.for_user.assert_called_once_with(USER_ID)


def test_get_without_wallet_is_none(svc, repo, wallets):
    repo.get.return_value = _row()
    wallets.for_user.return_value = None
    assert svc.get(USER_ID)["wallet"] is None


def test_get_unknown_is_404(svc, repo, wallets):
    repo.get.return_value = None
    with pytest.raises(AdminNotFound):
        svc.get(USER_ID)
    wallets.for_user.assert_not_called()


def test_devices_reuse_the_wave_1_device_service(svc, devices):
    devices.list.return_value = AdminPage(data=[{"id": 1}], total_count=1)
    assert svc.list_devices(USER_ID, PAGE).data == [{"id": 1}]
    devices.list.assert_called_once_with(PAGE, user_id=USER_ID)


def test_wallet_transactions_true_total(svc, repo):
    repo.wallet_transactions.return_value = Paged(rows=[{"id": "t1"}], total=31)
    page = svc.list_wallet_transactions(USER_ID, PAGE)
    assert page.data == [{"id": "t1"}] and page.total_count == 31
    repo.wallet_transactions.assert_called_once_with(USER_ID, PAGE)


def test_referrals_pass_filters(svc, repo):
    repo.referrals.return_value = Paged(rows=[], total=0)
    svc.list_referrals(USER_ID, PAGE, search="J", from_email="a@", referral_date=date(2026, 10, 1))
    repo.referrals.assert_called_once_with(USER_ID, PAGE, search="J", from_email="a@",
                                           referral_date=date(2026, 10, 1))


# ---------- ban ----------

def test_ban_updates_gotrue_then_audits(svc, repo, audit, client):
    repo.get.return_value = _row()
    until = datetime(2027, 10, 21, 10, 0, tzinfo=timezone.utc)
    client.auth.admin.update_user_by_id.return_value = SimpleNamespace(
        user=SimpleNamespace(id=USER_ID, email="jane@example.com", banned_until=until))

    result = svc.ban(USER_ID, AdminUserBan(reason="fraud"), CTX)

    client.auth.admin.update_user_by_id.assert_called_once_with(USER_ID, {"ban_duration": "8760h"})
    assert result == {"user_id": USER_ID, "email": "jane@example.com", "banned_until": until.isoformat(),
                      "hours_banned": 8760}
    args, kwargs = audit.log.call_args
    assert args == (CTX, AUTH_USERS_TABLE, "BAN")
    assert kwargs["record_id"] == USER_ID
    assert kwargs["hours_banned"] == 8760 and kwargs["ban_duration"] == "8760h"
    assert kwargs["banned_until"] == until.isoformat() and kwargs["user_email"] == "jane@example.com"
    assert kwargs["rows_affected"] == 1


def test_ban_custom_hours(svc, repo, client):
    repo.get.return_value = _row()
    svc.ban(USER_ID, AdminUserBan(hours=48, reason="r"), CTX)
    client.auth.admin.update_user_by_id.assert_called_once_with(USER_ID, {"ban_duration": "48h"})


def test_ban_already_banned_is_409_and_touches_nothing(svc, repo, audit, client):
    repo.get.return_value = _row(is_banned=True, banned_until=FUTURE)

    with pytest.raises(AdminConflict) as err:
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)

    assert err.value.conflict_code == "already_banned"
    assert err.value.extra == {"banned_until": FUTURE}
    client.auth.admin.update_user_by_id.assert_not_called()
    audit.log.assert_not_called()


def test_ban_unknown_user_is_404(svc, repo, client):
    repo.get.return_value = None
    with pytest.raises(AdminNotFound):
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)
    client.auth.admin.update_user_by_id.assert_not_called()


def test_ban_gotrue_404_is_404(svc, repo, client, audit):
    repo.get.return_value = _row()
    client.auth.admin.update_user_by_id.side_effect = AuthApiError("User not found", 404, "user_not_found")
    with pytest.raises(AdminNotFound):
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)
    audit.log.assert_not_called()


@pytest.mark.parametrize("error", [AuthApiError("boom", 500, "unexpected_failure"), RuntimeError("network")])
def test_ban_gotrue_failure_is_502_and_nothing_audited(svc, repo, client, audit, error):
    repo.get.return_value = _row()
    client.auth.admin.update_user_by_id.side_effect = error

    with pytest.raises(AdminUpstreamError) as err:
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)

    assert err.value.details == "AUTH_SERVICE_UNAVAILABLE"
    audit.log.assert_not_called()


def test_ban_audit_failure_lifts_the_ban_and_returns_502(svc, repo, client, audit):
    repo.get.return_value = _row()
    audit.log.side_effect = AdminUpstreamError("DATABASE_ERROR")

    with pytest.raises(AdminUpstreamError) as err:
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)

    assert err.value.details == "AUDIT_FAILED"
    calls = [c.args for c in client.auth.admin.update_user_by_id.call_args_list]
    assert calls == [(USER_ID, {"ban_duration": "8760h"}), (USER_ID, {"ban_duration": UNBAN_DURATION})]


def test_ban_compensation_failure_still_returns_502(svc, repo, client, audit):
    repo.get.return_value = _row()
    audit.log.side_effect = RuntimeError("db down")
    client.auth.admin.update_user_by_id.side_effect = [
        SimpleNamespace(user=SimpleNamespace(email="jane@example.com", banned_until=None)),
        RuntimeError("gotrue down"),
    ]
    with pytest.raises(AdminUpstreamError):
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)


# ---------- TEOS-111: sessions revoked on ban, no self-ban ----------

def test_ban_revokes_the_sessions_after_the_ban_and_audits_it(svc, repo, audit, client, revoker):
    repo.get.return_value = _row()
    order = []

    def ban(*args):
        order.append("ban")
        return SimpleNamespace(user=SimpleNamespace(email="jane@example.com", banned_until=None))

    client.auth.admin.update_user_by_id.side_effect = ban
    revoker.side_effect = lambda uid: order.append("revoke")
    audit.log.side_effect = lambda *a, **k: order.append("audit")

    svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)

    assert order == ["ban", "revoke", "audit"]
    revoker.assert_called_once_with(USER_ID)
    kwargs = audit.log.call_args.kwargs
    assert kwargs["sessions_revoked"] is True and kwargs["session_revoke_error"] is None


@pytest.mark.parametrize("error, reason", [
    (SessionRevokeError("jwt_secret_not_configured"), "jwt_secret_not_configured"),
    (RuntimeError("boom"), "unexpected:RuntimeError"),
])
def test_ban_revoke_failure_keeps_the_ban_audits_it_and_returns_502(svc, repo, audit, client, revoker,
                                                                    error, reason):
    repo.get.return_value = _row()
    revoker.side_effect = error

    with pytest.raises(AdminUpstreamError) as err:
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)

    assert err.value.details == SESSION_REVOKE_FAILED
    # the ban is applied once and never lifted again
    client.auth.admin.update_user_by_id.assert_called_once_with(USER_ID, {"ban_duration": "8760h"})
    audit.log.assert_called_once()
    kwargs = audit.log.call_args.kwargs
    assert kwargs["sessions_revoked"] is False and kwargs["session_revoke_error"] == reason


def test_ban_revoke_and_audit_failure_lifts_the_ban_and_reports_the_audit(svc, repo, audit, client, revoker):
    repo.get.return_value = _row()
    revoker.side_effect = SessionRevokeError("gotrue_error:AuthApiError")
    audit.log.side_effect = RuntimeError("db down")

    with pytest.raises(AdminUpstreamError) as err:
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)

    assert err.value.details == "AUDIT_FAILED"
    calls = [c.args for c in client.auth.admin.update_user_by_id.call_args_list]
    assert calls == [(USER_ID, {"ban_duration": "8760h"}), (USER_ID, {"ban_duration": UNBAN_DURATION})]


def test_ban_gotrue_failure_never_revokes(svc, repo, client, revoker):
    repo.get.return_value = _row()
    client.auth.admin.update_user_by_id.side_effect = RuntimeError("network")
    with pytest.raises(AdminUpstreamError):
        svc.ban(USER_ID, AdminUserBan(reason="r"), CTX)
    revoker.assert_not_called()


@pytest.mark.parametrize("target", [CTX.actor_id, CTX.actor_id.upper()])
def test_ban_self_is_409_and_touches_nothing(svc, repo, audit, client, revoker, target):
    with pytest.raises(AdminConflict) as err:
        svc.ban(target, AdminUserBan(reason="r"), CTX)

    assert err.value.conflict_code == CANNOT_BAN_SELF
    repo.get.assert_not_called()
    client.auth.admin.update_user_by_id.assert_not_called()
    revoker.assert_not_called()
    audit.log.assert_not_called()


def test_unban_self_is_409_and_touches_nothing(svc, repo, audit, client):
    with pytest.raises(AdminConflict) as err:
        svc.unban(CTX.actor_id, AdminUserUnban(reason="r"), CTX)

    assert err.value.conflict_code == CANNOT_BAN_SELF
    repo.get.assert_not_called()
    client.auth.admin.update_user_by_id.assert_not_called()
    audit.log.assert_not_called()


def test_unban_never_revokes_sessions(svc, repo, revoker):
    repo.get.return_value = _row(is_banned=True, banned_until=FUTURE)
    svc.unban(USER_ID, AdminUserUnban(reason="r"), CTX)
    revoker.assert_not_called()


def test_default_revoker_signs_out_globally_with_a_short_lived_user_token(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret-" + "x" * 32)
    client = MagicMock()

    revoke_user_sessions(client, USER_ID)

    token, scope = client.auth.admin.sign_out.call_args.args
    assert scope == "global"
    claims = pyjwt.decode(token, "test-secret-" + "x" * 32, algorithms=["HS256"], audience="authenticated")
    assert claims["sub"] == USER_ID and claims["role"] == "authenticated"
    assert "session_id" not in claims
    assert 0 < claims["exp"] - claims["iat"] <= 60


def test_default_revoker_without_secret_fails_without_calling_gotrue(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    client = MagicMock()

    with pytest.raises(SessionRevokeError) as err:
        revoke_user_sessions(client, USER_ID)

    assert err.value.reason == "jwt_secret_not_configured"
    client.auth.admin.sign_out.assert_not_called()


def test_default_revoker_wraps_a_gotrue_error(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret-" + "x" * 32)
    client = MagicMock()
    client.auth.admin.sign_out.side_effect = AuthApiError("Invalid JWT", 401, "bad_jwt")

    with pytest.raises(SessionRevokeError) as err:
        revoke_user_sessions(client, USER_ID)

    assert err.value.reason == "gotrue_error:AuthApiError"


def test_service_default_revoker_uses_the_client_factory(monkeypatch, repo, audit, client):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret-" + "x" * 32)
    repo.get.return_value = _row()
    service = UserAdminService(repo=repo, audit=audit, client_factory=lambda: client)

    service.ban(USER_ID, AdminUserBan(reason="r"), CTX)

    assert client.auth.admin.sign_out.call_args.args[1] == "global"
    assert audit.log.call_args.kwargs["sessions_revoked"] is True


# ---------- unban ----------

def test_unban_lifts_the_ban_and_audits_previous_banned_until(svc, repo, audit, client):
    repo.get.return_value = _row(is_banned=True, banned_until=FUTURE)

    result = svc.unban(USER_ID, AdminUserUnban(reason="cleared"), CTX)

    client.auth.admin.update_user_by_id.assert_called_once_with(USER_ID, {"ban_duration": UNBAN_DURATION})
    assert result == {"user_id": USER_ID, "email": "jane@example.com"}
    args, kwargs = audit.log.call_args
    assert args == (CTX, AUTH_USERS_TABLE, "UNBAN")
    assert kwargs["previous_banned_until"] == FUTURE
    assert kwargs["hours_banned"] is None and kwargs["ban_duration"] == UNBAN_DURATION


def test_unban_not_banned_is_409(svc, repo, client, audit):
    repo.get.return_value = _row(is_banned=False, banned_until="2020-01-01T00:00:00+00:00")  # lapsed ban

    with pytest.raises(AdminConflict) as err:
        svc.unban(USER_ID, AdminUserUnban(reason="r"), CTX)

    assert err.value.conflict_code == "not_banned"
    client.auth.admin.update_user_by_id.assert_not_called()
    audit.log.assert_not_called()


def test_unban_unknown_user_is_404(svc, repo):
    repo.get.return_value = None
    with pytest.raises(AdminNotFound):
        svc.unban(USER_ID, AdminUserUnban(reason="r"), CTX)


def test_unban_audit_failure_reapplies_the_remaining_ban(svc, repo, client, audit):
    repo.get.return_value = _row(is_banned=True, banned_until=FUTURE)
    audit.log.side_effect = RuntimeError("db down")

    with pytest.raises(AdminUpstreamError) as err:
        svc.unban(USER_ID, AdminUserUnban(reason="r"), CTX)

    assert err.value.details == "AUDIT_FAILED"
    first, second = [c.args for c in client.auth.admin.update_user_by_id.call_args_list]
    assert first == (USER_ID, {"ban_duration": UNBAN_DURATION})
    assert second[0] == USER_ID
    seconds = int(second[1]["ban_duration"].rstrip("s"))
    assert 29 * 24 * 3600 < seconds <= 30 * 24 * 3600


def test_unban_gotrue_failure_is_502(svc, repo, client, audit):
    repo.get.return_value = _row(is_banned=True, banned_until=FUTURE)
    client.auth.admin.update_user_by_id.side_effect = AuthApiError("boom", 502, "unexpected_failure")
    with pytest.raises(AdminUpstreamError):
        svc.unban(USER_ID, AdminUserUnban(reason="r"), CTX)
    audit.log.assert_not_called()


def test_remaining_ban_duration():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    assert remaining_ban_duration("2026-10-21T13:00:00+00:00", now) == "3600s"
    assert remaining_ban_duration("2026-10-21T13:00:00Z", now) == "3600s"
    assert remaining_ban_duration(now + timedelta(seconds=1.2), now) == "2s"
    assert remaining_ban_duration("2026-10-21T11:00:00+00:00", now) is None
    assert remaining_ban_duration(None, now) is None
    assert remaining_ban_duration("garbage", now) is None


# ---------- schemas ----------

def test_ban_body_defaults_and_trims():
    body = AdminUserBan(reason="  fraud ")
    assert body.hours == 8760 and body.reason == "fraud"


@pytest.mark.parametrize("kwargs", [{"hours": 0, "reason": "r"}, {"hours": 87601, "reason": "r"},
                                    {"reason": " "}, {"reason": "r", "extra": 1}])
def test_ban_body_rejects(kwargs):
    with pytest.raises(ValueError):
        AdminUserBan(**kwargs)


# ---------- repositories ----------

def _repo_with_rpc(cls, result):
    repo = cls.__new__(cls)
    repo.table_name = "users_copy"
    repo.rpc = MagicMock(return_value=result)
    return repo


def test_repo_search_calls_admin_search_users():
    repo = _repo_with_rpc(UserAdminRepo, {"data": [_row()], "total": 1, "facets": {"all": 1, "banned": 0, "active": 1}})

    result = repo.search("  ja ", "banned", PAGE)

    repo.rpc.assert_called_once_with("admin_search_users", {
        "p_search": "ja", "p_ban_status": "banned", "p_limit": 10, "p_offset": 10, "p_id": None,
        "p_sort": "created_at", "p_dir": "desc"})
    assert result.rows == [_row()] and result.total == 1
    assert result.facets == {"all": 1, "banned": 0, "active": 1}


def test_repo_search_passes_sort_and_direction():
    repo = _repo_with_rpc(UserAdminRepo, {"data": [], "total": 0, "facets": {}})
    repo.search(None, "all", PAGE, sort="email", direction="asc")
    params = repo.rpc.call_args.args[1]
    assert (params["p_sort"], params["p_dir"]) == ("email", "asc")


def test_repo_search_blank_search_is_null_and_tolerates_a_wrapped_result():
    repo = _repo_with_rpc(UserAdminRepo, [{"data": [], "total": 0, "facets": {}}])
    result = repo.search("   ", "all", PageParams())
    assert repo.rpc.call_args.args[1]["p_search"] is None
    assert result.rows == [] and result.total == 0 and result.facets == {}


def test_repo_get_filters_by_id():
    repo = _repo_with_rpc(UserAdminRepo, {"data": [_row()], "total": 1, "facets": {}})
    assert repo.get(USER_ID) == _row()
    repo.rpc.assert_called_once_with("admin_search_users", {
        "p_search": None, "p_ban_status": "all", "p_limit": 1, "p_offset": 0, "p_id": USER_ID})


def test_repo_get_unknown_is_none():
    repo = _repo_with_rpc(UserAdminRepo, {"data": [], "total": 0, "facets": {}})
    assert repo.get(USER_ID) is None


def test_repo_wallet_transactions_rpc():
    repo = _repo_with_rpc(UserAdminRepo, {"data": [{"id": "t1"}], "total": 9})
    result = repo.wallet_transactions(USER_ID, PAGE)
    repo.rpc.assert_called_once_with("admin_wallet_transactions", {"p_user_id": USER_ID, "p_limit": 10, "p_offset": 10})
    assert result == Paged(rows=[{"id": "t1"}], total=9)


def test_repo_referrals_rpc_by_name_with_the_plan_parameters():
    repo = _repo_with_rpc(UserAdminRepo, {"items": [{"id": "o1"}], "total_count": 3})

    result = repo.referrals(USER_ID, PAGE, search=" J ", from_email="", referral_date=date(2026, 10, 1))

    repo.rpc.assert_called_once_with("admin_referral_transactions", {
        "p_user_id": USER_ID, "p_search": "J", "p_from_email": None, "p_date": "2026-10-01",
        "p_limit": 10, "p_offset": 10})
    assert result == Paged(rows=[{"id": "o1"}], total=3)


def test_wallet_repo_for_user():
    repo = UserWalletAdminRepo.__new__(UserWalletAdminRepo)
    repo.table_name = "user_wallet"
    query = MagicMock()
    query.limit.return_value.execute.return_value = SimpleNamespace(data=[{"amount": "2.50", "currency": "USD"}])
    repo.filters = MagicMock(return_value=query)
    repo._run = lambda call: call()
    repo.table = MagicMock()

    assert repo.for_user(USER_ID) == {"amount": 2.5, "currency": "USD"}
    repo.table.select.assert_called_once_with("amount,currency")
    assert repo.filters.call_args.kwargs == {"eq": {"user_id": USER_ID}}

    query.limit.return_value.execute.return_value = SimpleNamespace(data=[])
    assert repo.for_user(USER_ID) is None
