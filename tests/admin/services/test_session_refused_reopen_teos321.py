"""TEOS-321 reopened (QA retest 2026-11-17).

1. A refused GET /users wrote no ``session_refused`` row while /auth/me and /admins did, for the same session. The
   rate cap was keyed on (session, reason): once one route was audited, the session's refusals on every other route
   were skipped for 5 minutes (per API process, so which route showed up depended on the process). The route is now
   part of the key: each route a refused session tries is audited once per window.
2. Every row had ``operation_details.reason`` null: the reason was sent as a detail, and ``admin_write_audit``
   writes its own ``reason`` key last, from the audit context. The reason now goes in the context.
"""
from types import SimpleNamespace

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies import admin_security
from app.dependencies.admin_security import resolve_admin
from app.repo.admin.base import AuditContext
from tests.admin.conftest import make_auth_user
from tests.admin.services.test_session_refused_audit_teos321 import SECRET, _genuine, _request, audits  # noqa: F401
from tests.admin.services.test_trusted_devices_signin import _get_user, _raises, session_token
from tests.admin.services.test_trusted_devices_signin import (  # noqa: F401  (fixtures)
    audit, clock, devices, lookup, otp_repo, service, supabase, verification_repo, verifications)

ME = "/api/v1/admin/auth/me"
USERS = "/api/v1/admin/users"
ADMINS = "/api/v1/admin/admins"


def _refused_on(path, session_id="sess-1"):
    _raises(lambda: resolve_admin(session_token(session_id), _request(path)), 401, "device_session_required")


# ---------- 1. every route shows up ----------

def test_qa_retest_same_session_on_me_users_admins_writes_one_row_per_route(supabase_auth, lookup, audits):
    """QA's retest: a token refreshed directly at Supabase, sent to /auth/me, /users and /admins seconds apart."""
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1", last_token_iat=500)  # session_token's iat is 1000: refreshed outside the API

    for path in (ME, USERS, ADMINS):
        _refused_on(path)

    assert [(a["path"], a["reason"], a["session_id"]) for a in audits] == [
        (ME, "token_refreshed_outside_api", "sess-1"),
        (USERS, "token_refreshed_outside_api", "sess-1"),
        (ADMINS, "token_refreshed_outside_api", "sess-1"),
    ]


def test_a_polling_page_is_still_capped_per_route(supabase_auth, lookup, audits):
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1", last_token_iat=500)

    for _ in range(5):
        _refused_on(USERS)
        _refused_on(ME)

    assert sorted(a["path"] for a in audits) == [ME, USERS]


def test_the_cap_still_separates_sessions(supabase_auth, lookup, audits):
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1", last_token_iat=500)
    lookup.add(session_id="sess-2", last_token_iat=500)

    _refused_on(USERS, "sess-1")
    _refused_on(USERS, "sess-2")
    _refused_on(USERS, "sess-1")

    assert [a["session_id"] for a in audits] == ["sess-1", "sess-2"]


def test_through_the_real_routes(api_client, supabase_auth, lookup, audits, monkeypatch):
    """/users goes through the same dependency as /auth/me and /admins (require_permission -> admin_user)."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)  # importing the app may load a local .env
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_ADMIN))
    lookup.add(session_id="sess-1", last_token_iat=500)
    headers = {"Authorization": f"Bearer {_genuine('sess-1')}", "CF-Connecting-IP": "9.9.9.9"}

    for path in (ME, USERS, ADMINS, USERS):
        assert api_client.get(path, headers=headers).status_code == 401

    assert [(a["path"], a["reason"], a["ip"]) for a in audits] == [
        (ME, "token_refreshed_outside_api", "9.9.9.9"),
        (USERS, "token_refreshed_outside_api", "9.9.9.9"),
        (ADMINS, "token_refreshed_outside_api", "9.9.9.9"),
    ]


# ---------- 2. the reason is stored ----------

def test_the_reason_reaches_admin_write_audit_as_p_reason(monkeypatch):
    """admin_write_audit stores operation_details.reason from p_reason (= the context's reason), after the details:
    the refusal reason must be sent there, not as a detail."""
    calls = []

    class FakeRepo:
        def rpc(self, name, params):
            calls.append((name, params))
            return {}

    from app.repo.admin.audit_log_repo import AuditLogRepo
    monkeypatch.setattr(AuditLogRepo, "__init__", lambda self: None)
    monkeypatch.setattr(AuditLogRepo, "rpc", lambda self, name, params: FakeRepo().rpc(name, params))
    row = {"id": "dev-1", "session_id": "sess-1", "revoked_at": None, "revoke_reason": None}

    admin_security._default_refusal_audit(row, "admin-1", "9.9.9.9", "token_refreshed_outside_api",
                                          code="device_session_required", path=USERS, session_id="sess-1")

    (name, params), = calls
    assert name == "admin_write_audit"
    assert params["p_reason"] == "token_refreshed_outside_api"
    assert params["p_ip"] == "9.9.9.9"
    assert "reason" not in params["p_details"]  # would be overwritten in SQL anyway
    assert params["p_details"]["path"] == USERS and params["p_details"]["action"] == "session_refused"


def test_audit_context_reason_is_what_the_sql_writes():
    """Guard: the field the SQL function reads is AuditContext.reason."""
    assert AuditContext(actor_id="a", ip=None, reason="device_revoked").reason == "device_revoked"
