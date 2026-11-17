"""TEOS-340: an admin whose access was revoked (Admins page > Revoke access) is told "Admin access was removed from
this account.", not "This device was signed out.".

Revoke access removes the role AND revokes every device row (reason ``access_revoked``), which ends the Supabase
sessions. The admin's next request is then refused by Supabase or by the device check, before the role check
that answers 403. Those device refusals now answer 401 ``access_removed`` for that reason; the portal signs out
with the "access removed" message on that code. Every other revoke reason keeps 401 ``device_revoked``.
"""
from types import SimpleNamespace

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.config.constants import ErrorMessages
from app.dependencies import admin_security
from app.dependencies.admin_security import resolve_admin
from app.services.admin import trusted_device_service
from tests.admin.conftest import make_auth_user
from tests.admin.services.test_session_refused_audit_teos321 import (PATH, REFRESH_PATH, SECRET, _genuine,
                                                                    _request, audits)  # noqa: F401  (fixture)
from tests.admin.services.test_trusted_devices_signin import (ADMIN_ID, DEVICE, _get_user, _raises, auth_response,
                                                             session_token)
from tests.admin.services.test_trusted_devices_signin import (  # noqa: F401  (fixtures)
    audit, clock, devices, lookup, otp_repo, service, supabase, verification_repo, verifications)

ACCESS_REVOKED = trusted_device_service.REASON_ACCESS_REVOKED


def _revoked(store, reason=ACCESS_REVOKED, session_id="sess-1"):
    row = store.add(session_id=session_id, revoked=True)
    row["revoke_reason"] = reason
    return row


def test_the_reason_matches_the_one_revoke_access_writes():
    assert admin_security.ACCESS_REVOKED_REASON == trusted_device_service.REASON_ACCESS_REVOKED == "access_revoked"


# ---------- per request ----------

def test_qa_steps_supabase_session_ended_without_jwt_secret_gets_access_removed(supabase_auth, lookup, audits,
                                                                                 monkeypatch):
    """QA's steps: Revoke access ended the session, the admin clicks Users: 401 access_removed (no secret on the
    API: claims read unchecked only to pick the code, no audit row)."""
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    supabase_auth.get_user.side_effect = RuntimeError("session_not_found")
    _revoked(lookup)

    with pytest.raises(admin_security.AdminError) as err:
        resolve_admin(session_token("sess-1"), _request())

    assert err.value.code == 401 and '"access_removed"' in err.value.details
    assert err.value.name == ErrorMessages.ADMIN_ACCESS_REMOVED
    assert audits == []


def test_supabase_session_ended_with_jwt_secret_is_audited_as_access_revoked(supabase_auth, lookup, audits,
                                                                              monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    supabase_auth.get_user.side_effect = RuntimeError("session_not_found")
    row = _revoked(lookup)

    _raises(lambda: resolve_admin(_genuine("sess-1"), _request()), 401, "access_removed")

    assert [(a["device_id"], a["reason"], a["code"], a["path"]) for a in audits] == \
        [(row["id"], "access_revoked", "access_removed", PATH)]


def test_session_still_open_at_supabase_with_the_role_back_gets_access_removed(supabase_auth, lookup, audits):
    """The device row decides, even if Supabase still accepts the token (e.g. the role was given back)."""
    _get_user(supabase_auth)
    row = _revoked(lookup)

    _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 401, "access_removed")

    assert [(a["device_id"], a["reason"], a["code"]) for a in audits] == [(row["id"], "access_revoked",
                                                                            "access_removed")]


def test_session_still_open_without_the_role_keeps_the_403(supabase_auth, lookup):
    """Unchanged: when Supabase accepts the token, the role check (403) still comes first."""
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(None, user_id=ADMIN_ID))
    _revoked(lookup)

    _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 403)


@pytest.mark.parametrize("reason", ["revoked_by_user", "signed_out_others", "revoked_by_super_admin",
                                    "trusted_limit", "password_change", "password_reset", None])
@pytest.mark.parametrize("supabase_ended", [True, False])
def test_every_other_revoke_reason_keeps_device_revoked(supabase_auth, lookup, audits, monkeypatch, reason,
                                                        supabase_ended):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    if supabase_ended:
        supabase_auth.get_user.side_effect = RuntimeError("session_not_found")
    else:
        _get_user(supabase_auth)
    _revoked(lookup, reason=reason)

    _raises(lambda: resolve_admin(session_token("sess-1"), _request()), 401, "device_revoked")


def test_api_route_answers_401_access_removed_with_its_message(api_client, supabase_auth, lookup, audits,
                                                               monkeypatch):
    """Through the real dependency and error handler: the code and the English message."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    supabase_auth.get_user.side_effect = RuntimeError("session_not_found")
    _revoked(lookup)

    response = api_client.get(PATH, headers={"Authorization": f"Bearer {_genuine('sess-1')}"})

    assert response.status_code == 401
    body = response.json()
    assert '"access_removed"' in body["developerMessage"]
    assert body["message"] == "Admin access was removed from this account."


# ---------- refresh ----------

def test_refresh_of_a_session_whose_access_was_revoked_gets_access_removed(service, supabase, devices, audits):
    """A refresh token still known (e.g. the session could not be ended) is refused before use, with the same
    code."""
    _revoked(devices, session_id="sess-9")
    supabase.refresh_response = auth_response("sess-9")

    _raises(lambda: service.refresh("refresh-token", DEVICE, "9.9.9.9"), 401, "access_removed")

    assert supabase.refreshed == []
    assert [(a["reason"], a["code"], a["path"]) for a in audits] == [("access_revoked", "access_removed",
                                                                      REFRESH_PATH)]
