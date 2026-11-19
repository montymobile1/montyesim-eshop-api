"""TEOS-341: who made an audited change stays readable after the account is deleted.

``admin_write_audit`` saves the actor's email, phone and role in ``audit_log.changed_by_info`` when it writes a row.
The audit log and the settings history show, for ``changed_by``:

- account still in ``users_copy``: its current email and phone, as before, plus the role saved with the row;
- account not found in ``users_copy``: the email, phone and role saved with the row (not marked deleted: a missing
  ``users_copy`` row does not prove the account is gone);
- no account and nothing saved (rows written before TEOS-341): null, the portal's "Deleted user".
"""
import json
import re
from pathlib import Path

import pytest

from app.repo.admin.audit_log_repo import AuditSearch
from app.services.admin.audit_query_service import _saved_actor, actor
from tests.admin.api.test_audit_log_api import ACTOR, API_ROW, GONE, URL, as_super, repos  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[3]
DDL = ROOT / "supabase_ddl.sql"
SAVED = {"email": "old.admin@x.com", "phone": "+96170000000", "role": "super_admin"}
USERS = {ACTOR: {"id": ACTOR, "email": "boss@example.com", "msisdn": None}}


# ---------- the actor ----------

def test_existing_account_keeps_its_current_email_and_gets_the_saved_role():
    found = actor(ACTOR, USERS, SAVED)

    assert (found.id, found.email, found.role) == (ACTOR, "boss@example.com", "super_admin")
    assert found.phone == "+96170000000"  # no current phone: the saved one


def test_existing_account_without_email_falls_back_to_the_saved_one():
    users = {ACTOR: {"id": ACTOR, "email": None, "msisdn": "+96171111111"}}

    found = actor(ACTOR, users, SAVED)

    assert (found.email, found.phone) == ("old.admin@x.com", "+96171111111")


def test_account_not_found_shows_what_was_saved_with_the_row_without_a_deleted_mark():
    gone = actor(GONE, USERS, SAVED)

    assert gone.model_dump() == {"id": GONE, "email": "old.admin@x.com", "phone": "+96170000000",
                                 "role": "super_admin"}


def test_phone_only_admin_that_was_deleted():
    gone = actor(GONE, USERS, {"email": None, "phone": "+96170000000", "role": "admin"})

    assert (gone.email, gone.phone, gone.role) == (None, "+96170000000", "admin")


@pytest.mark.parametrize("info", [None, {}, {"email": None, "phone": "", "role": "  "}, "not json", 42, []])
def test_deleted_account_with_nothing_saved_is_null_as_before(info):
    assert actor(GONE, USERS, info) is None


def test_no_actor_id_is_null():
    assert actor(None, USERS, SAVED) is None


def test_saved_info_may_come_as_json_text():
    assert _saved_actor(json.dumps(SAVED)) == SAVED
    assert _saved_actor({"email": " a@x.com ", "phone": True, "role": "admin", "other": "x"}) == \
        {"email": "a@x.com", "role": "admin"}


# ---------- through the API ----------

def test_audit_log_list_and_detail(api_client, as_super, repos):
    deleted_row = {**API_ROW, "id": 21, "changed_by": GONE, "changed_by_info": SAVED}
    old_row = {**API_ROW, "id": 22, "changed_by": GONE}
    live_row = {**API_ROW, "changed_by_info": {"email": "boss@example.com", "phone": None, "role": "admin"}}
    repos["audit"].search.return_value = AuditSearch(rows=[live_row, deleted_row, old_row], total=3,
                                                     facets_by_table={"bundle": 3})

    live, deleted, old = api_client.get(URL).json()["data"]["data"]

    assert live["changed_by"] == {"id": ACTOR, "email": "boss@example.com", "phone": None, "role": "admin"}
    assert deleted["changed_by"] == {"id": GONE, "email": "old.admin@x.com", "phone": "+96170000000",
                                     "role": "super_admin"}
    assert old["changed_by"] is None  # the portal shows "Deleted user"
    assert "changed_by_info" not in deleted  # only the projected actor is returned

    repos["audit"].get.return_value = deleted_row
    detail = api_client.get(f"{URL}/21").json()["data"]
    assert detail["changed_by"]["email"] == "old.admin@x.com" and "deleted" not in detail["changed_by"]


# ---------- the SQL ----------

def _write_audit_body(sql: str) -> str:
    start = sql.index("CREATE OR REPLACE FUNCTION public.admin_write_audit(")
    return sql[start:sql.index("$$;", sql.index("AS $$", start)) + 3]


def test_the_column_is_added_without_touching_existing_rows():
    sql = DDL.read_text(encoding="utf-8")
    assert "ALTER TABLE public.audit_log\n    ADD COLUMN IF NOT EXISTS changed_by_info jsonb;" in sql
    assert sql.index("ADD COLUMN IF NOT EXISTS changed_by_info") < sql.index(
        "CREATE OR REPLACE FUNCTION public.admin_write_audit(")


def test_admin_write_audit_saves_the_actor_from_auth_users_never_from_the_caller():
    body = _write_audit_body(DDL.read_text(encoding="utf-8"))

    assert "SET search_path = ''" in body
    assert re.search(r"FROM auth\.users u\s+WHERE u\.id = p_actor", body)
    for field in ("'email', nullif(btrim(u.email), '')",
                  "nullif(btrim(u.phone), '')",
                  "u.raw_user_meta_data ->> 'msisdn'",
                  "'role',  nullif(btrim(u.raw_app_meta_data ->> 'role'), '')"):
        assert field in body
    assert "operation_details, changed_by_info)" in body and "v_details,\n         v_actor)" in body
    assert "p_details ->> 'email'" not in body and "changed_by_info'" not in body


def test_signature_grants_and_comment_are_unchanged():
    sql = DDL.read_text(encoding="utf-8")
    signature = "public.admin_write_audit(text, text, text, jsonb, jsonb, uuid, text, text, jsonb)"
    assert f"GRANT EXECUTE ON FUNCTION {signature} TO service_role;" in sql
    assert f"COMMENT ON FUNCTION {signature} IS" in sql


@pytest.mark.parametrize("name", ["341-ddl.sql", "new_change_ddl.sql"])
def test_lead_files_hold_the_same_function(name):
    path = ROOT / "migrations" / "admin" / name
    if not path.exists():
        pytest.skip("lead SQL files are local only (gitignored)")
    lead = path.read_text(encoding="utf-8")
    start = lead.index("-- TEOS-341 (2026-11-17)") if name == "new_change_ddl.sql" else 0  # its own section
    assert _write_audit_body(lead[start:]) == \
        _write_audit_body(DDL.read_text(encoding="utf-8"))
    assert "ADD COLUMN IF NOT EXISTS changed_by_info jsonb;" in lead
