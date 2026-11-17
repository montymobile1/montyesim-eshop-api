"""TEOS-321: every refused admin session writes one ``session_refused`` audit row (reason, 401 code, admin, device,
session, IP, route), rate capped per session and reason. The four refusal paths of the bug report:
a revoked device, an expired trust, a token refreshed directly at Supabase, a refresh from another browser.

The audit hook (``admin_security.refusal_audit``) is captured here; conftest stubs it everywhere else.
"""
from types import SimpleNamespace

import jwt
import pytest
from starlette.requests import Request

from app.dependencies import admin_security
from app.dependencies.admin_security import RefusalDedupe, audit_refused_session, resolve_admin
from tests.admin.conftest import make_auth_user
from tests.admin.services.test_trusted_devices_signin import (ADMIN_ID, DEVICE, OTHER_ID, _get_user, _raises,
                                                             auth_response, device_info, session_token)
from tests.admin.services.test_trusted_devices_signin import (  # noqa: F401  (fixtures)
    audit, clock, devices, lookup, otp_repo, service, supabase, verification_repo, verifications)
from app.config.admin_permissions import ROLE_ADMIN

SECRET = "the-real-secret-" + "s" * 32
PATH = "/api/v1/admin/me/devices"
REFRESH_PATH = "/api/v1/admin/auth/refresh"


@pytest.fixture
def audits(monkeypatch):
    rows = []
    monkeypatch.setattr(admin_security, "refusal_audit",
                        lambda row, admin_id, ip, reason, **details: rows.append(
                            {"device_id": row["id"] if row else None, "admin_id": admin_id, "ip": ip,
                             "reason": reason, **details}))
    return rows


def _request(path=PATH, ip="9.9.9.9") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
                    "headers": [(b"cf-connecting-ip", ip.encode())], "client": ("10.0.0.1", 1234),
                    "server": ("api", 443), "scheme": "https", "root_path": ""})


def _genuine(session_id="sess-1", sub=ADMIN_ID, exp=4102444800, secret=SECRET) -> str:
    """A token signed with the project's JWT secret (what Supabase issues)."""
    return jwt.encode({"sub": sub, "aud": "authenticated", "exp": exp, "session_id": session_id, "iat": 1000,
                       "amr": [{"method": "password", "timestamp": 1}]}, secret, algorithm="HS256")


# ---------- per request (the /admin/* dependency) ----------

def test_revoked_device_whose_session_still_exists_is_audited_with_ip_and_route(supabase_auth, lookup, audits):
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1", revoked=True)

    _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 401, "device_revoked")

    assert audits == [{"device_id": row["id"], "admin_id": ADMIN_ID, "ip": "9.9.9.9", "reason": "device_revoked",
                       "code": "device_revoked", "path": PATH, "session_id": "sess-1"}]


def test_revoked_device_whose_supabase_session_was_ended_is_audited(supabase_auth, lookup, audits, monkeypatch):
    """QA step 1: revoking ends the Supabase session, so Supabase refuses the token before the device check.
    A genuine token of a revoked device row is still audited."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    supabase_auth.get_user.side_effect = RuntimeError("session_not_found")
    row = lookup.add(session_id="sess-1", revoked=True)

    # TEOS-332: the answer names the revoked device
    _raises(lambda: resolve_admin(_genuine("sess-1"), _request()), 401, "device_revoked")

    assert [(a["device_id"], a["reason"], a["path"], a["ip"]) for a in audits] == \
        [(row["id"], "device_revoked", PATH, "9.9.9.9")]


@pytest.mark.parametrize("case, answer", [
    ("forged", "INVALID_OR_EXPIRED_TOKEN"),
    ("expired", "device_revoked"),     # genuine but expired: the code still says revoked, no audit
    ("no_secret", "device_revoked"),   # TEOS-332 option A: claims read unchecked, only for the code
    ("row_active", "INVALID_OR_EXPIRED_TOKEN"),
    ("no_row", "INVALID_OR_EXPIRED_TOKEN"),
    ("other_admin", "INVALID_OR_EXPIRED_TOKEN"),
])
def test_supabase_refusal_is_not_audited_unless_genuine_and_revoked(supabase_auth, lookup, audits, monkeypatch, case,
                                                                    answer):
    if case != "no_secret":
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    supabase_auth.get_user.side_effect = RuntimeError("session_not_found")
    if case not in ("row_active", "no_row"):
        lookup.add(session_id="sess-1", revoked=True, admin_id=OTHER_ID if case == "other_admin" else ADMIN_ID)
    elif case == "row_active":
        lookup.add(session_id="sess-1")
    token = {"forged": _genuine(secret="another-secret-" + "x" * 32), "expired": _genuine(exp=1)}.get(case, _genuine())

    with pytest.raises(admin_security.AdminError) as err:
        resolve_admin(token, _request())

    assert err.value.code == 401
    if answer == "device_revoked":
        assert '"device_revoked"' in err.value.details
    else:
        assert err.value.details == answer
    assert audits == []


# ---------- TEOS-332: a revoked device is told so ----------

def test_teos332_revoked_device_without_jwt_secret_gets_device_revoked(supabase_auth, lookup, audits):
    """QA's steps: B was revoked from A (session ended), B's next request: 401 device_revoked, which the portal
    shows as "This device was signed out." (no secret on the API: no audit row)."""
    supabase_auth.get_user.side_effect = RuntimeError("session_not_found")
    lookup.add(session_id="sess-1", revoked=True)

    _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 401, "device_revoked")
    assert audits == []


def test_teos332_unreadable_token_or_lookup_failure_stays_generic(supabase_auth, monkeypatch, audits):
    supabase_auth.get_user.side_effect = RuntimeError("session_not_found")

    def broken(session_id):
        raise RuntimeError("db down")
    monkeypatch.setattr(admin_security, "session_device_lookup", broken)

    for token in ("not-a-jwt", session_token(None), session_token("sess-1")):
        with pytest.raises(admin_security.AdminError) as err:
            resolve_admin(token, _request())
        assert err.value.code == 401 and err.value.details == "INVALID_OR_EXPIRED_TOKEN"


def test_expired_trust_is_audited(supabase_auth, lookup, audits):
    """QA step 2."""
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1")
    row["trusted_until"] = "2000-01-01T00:00:00+00:00"

    _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 401, "device_trust_expired")

    assert [(a["device_id"], a["reason"], a["code"]) for a in audits] == \
        [(row["id"], "device_trust_expired", "device_trust_expired")]


@pytest.mark.parametrize("path", ["/api/v1/admin/me/devices", "/api/v1/admin/auth/me", "/api/v1/admin/users",
                                  "/api/v1/admin/admins"])
def test_token_refreshed_directly_at_supabase_is_audited_on_every_route(supabase_auth, lookup, audits, path):
    """QA step 3."""
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1", last_token_iat=1000)

    _raises(lambda: resolve_admin(session_token("sess-1", iat=1001), _request(path)), 401, "device_session_required")

    assert [(a["device_id"], a["reason"], a["code"], a["path"]) for a in audits] == \
        [(row["id"], "token_refreshed_outside_api", "device_session_required", path)]


def test_session_without_device_row_is_audited(supabase_auth, lookup, audits):
    _get_user(supabase_auth)

    _raises(lambda: resolve_admin(session_token("direct"), _request()), 401, "device_session_required")

    assert [(a["device_id"], a["reason"], a["session_id"]) for a in audits] == [(None, "no_device_row", "direct")]


def test_accepted_session_writes_nothing(supabase_auth, lookup, audits):
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1")

    resolve_admin(session_token("sess-1"), _request())

    assert audits == []


# ---------- refresh ----------

@pytest.mark.parametrize("setup, info, reason, code", [
    ("revoked", DEVICE, "device_revoked", "device_revoked"),
    ("expired", DEVICE, "device_trust_expired", "device_trust_expired"),
    ("active", device_info(browser="Firefox"), "device_mismatch", "device_session_required"),  # QA step 4
    ("none", DEVICE, "no_device_row", "device_session_required"),
])
def test_each_refused_refresh_is_audited(service, supabase, devices, audits, setup, info, reason, code):
    if setup != "none":
        row = devices.add(session_id="sess-9", revoked=setup == "revoked")
        if setup == "expired":
            row["trusted_until"] = "2000-01-01T00:00:00+00:00"
    supabase.refresh_response = auth_response("sess-9")

    _raises(lambda: service.refresh("refresh-token", info, "9.9.9.9"), 401, code)

    assert [(a["reason"], a["code"], a["path"], a["ip"], a["session_id"]) for a in audits] == \
        [(reason, code, REFRESH_PATH, "9.9.9.9", "sess-9")]


# ---------- rate cap ----------

def test_same_session_and_reason_is_audited_once_per_window(supabase_auth, lookup, audits):
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1", revoked=True)

    for _ in range(5):  # a polling page
        _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 401, "device_revoked")
    assert len(audits) == 1

    row["revoked_at"] = None
    row["trusted_until"] = "2000-01-01T00:00:00+00:00"
    _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 401, "device_trust_expired")
    assert [a["reason"] for a in audits] == ["device_revoked", "device_trust_expired"]  # another reason


def test_rate_cap_window_and_its_setting(monkeypatch):
    now = [0.0]
    dedupe = RefusalDedupe(window_seconds=300, clock=lambda: now[0])
    assert dedupe.first(("s", "r")) and not dedupe.first(("s", "r"))
    assert dedupe.first(("other", "r"))
    now[0] = 301
    assert dedupe.first(("s", "r"))

    monkeypatch.setenv("ADMIN_SESSION_REFUSED_AUDIT_WINDOW_SECONDS", "0")  # 0 = no cap
    uncapped = RefusalDedupe(clock=lambda: 0.0)
    assert uncapped.first("k") and uncapped.first("k")
    monkeypatch.setenv("ADMIN_SESSION_REFUSED_AUDIT_WINDOW_SECONDS", "nonsense")  # falls back to 300
    capped = RefusalDedupe(clock=lambda: 0.0)
    assert capped.first("k") and not capped.first("k")


def test_audit_failure_still_gives_the_401(supabase_auth, lookup, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("audit store down")
    monkeypatch.setattr(admin_security, "refusal_audit", broken)
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1")

    _raises(lambda: resolve_admin(session_token("no-row"), _request()), 401, "device_session_required")


# ---------- the stored row ----------

def test_default_audit_row_fields(monkeypatch):
    logged = []

    class FakeAuditService:
        def log(self, ctx, table, operation, **kwargs):
            logged.append((ctx.actor_id, ctx.ip, ctx.reason, table, operation, kwargs))

    monkeypatch.setattr("app.services.admin.audit_service.AuditService", FakeAuditService)
    row = {"id": "dev-1", "session_id": "sess-1", "revoked_at": "2026-11-12T10:00:00+00:00",
           "revoke_reason": "user_revoked"}

    admin_security._default_refusal_audit(row, ADMIN_ID, "9.9.9.9", "device_revoked", code="device_revoked",
                                          path=PATH, session_id="sess-1")
    admin_security._default_refusal_audit(None, ADMIN_ID, None, "no_device_row", code="device_session_required",
                                          path=PATH, session_id="direct")

    # TEOS-321 reopen: the reason is the context's (admin_write_audit stores operation_details.reason from it)
    assert logged[0] == (ADMIN_ID, "9.9.9.9", "device_revoked", "admin_devices", "UPDATE",
                         {"record_id": "dev-1", "action": "session_refused",
                          "admin_id": ADMIN_ID, "device_id": "dev-1", "revoke_reason": "user_revoked",
                          "code": "device_revoked", "path": PATH, "session_id": "sess-1"})
    assert logged[1][2] == "no_device_row"
    assert logged[1][5]["record_id"] == "direct" and logged[1][5]["device_id"] is None


def test_api_route_passes_the_ip_and_route(api_client, supabase_auth, lookup, audits, monkeypatch):
    """Through the real dependency: GET /me/devices with a revoked device."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)  # importing the app may load a local .env
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_ADMIN, user_id=ADMIN_ID))
    lookup.add(session_id="sess-1", revoked=True)

    response = api_client.get(PATH, headers={"Authorization": f"Bearer {_genuine('sess-1')}",
                                             "CF-Connecting-IP": "9.9.9.9"})

    assert response.status_code == 401
    assert [(a["reason"], a["path"], a["ip"]) for a in audits] == [("device_revoked", PATH, "9.9.9.9")]
