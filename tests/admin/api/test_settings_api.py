"""HTTP contract of /api/v1/admin/settings and /settings/history (MS-5091, MS-5046).

The real SettingsAdminService runs; only its repositories are mocked.
"""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminConflict, AdminUpstreamError
from app.repo.admin.audit_log_repo import AuditSearch
from app.repo.admin.base import Paged
from app.services.admin.settings_service import SettingsAdminService
from tests.admin.conftest import make_admin

URL = "/api/v1/admin/settings"
ACTOR = "11111111-1111-1111-1111-111111111111"
OTHER = "33333333-3333-3333-3333-333333333333"


def _as(role):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(role)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_super():
    yield from _as(ROLE_SUPER_ADMIN)


@pytest.fixture
def as_admin():
    yield from _as(ROLE_ADMIN)


@pytest.fixture
def repos(monkeypatch):
    from app.api.v1.admin import settings as routes
    settings, audit, users = MagicMock(), MagicMock(), MagicMock()
    users.by_ids.return_value = {ACTOR: {"id": ACTOR, "email": "boss@example.com", "msisdn": None}}
    monkeypatch.setattr(routes, "service", SettingsAdminService(settings=settings, audit=audit, users=users))
    return {"settings": settings, "audit": audit, "users": users}


def _code(response):
    return json.loads(response.json()["developerMessage"])["code"]


# ---------------------------------------------------------------- GET /settings

def test_list_settings_paged_with_exact_key_filter(api_client, as_super, repos):
    repos["settings"].search.return_value = Paged(rows=[
        {"id": 1, "key": "FORBIDDEN_COUNTRIES", "value": "XX", "created_at": "2026-10-01T10:00:00+00:00"},
    ], total=31)

    response = api_client.get(URL, params={"key": "FORBIDDEN_COUNTRIES", "page": 2, "page_size": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 31
    assert body["data"]["total_count"] == 31
    assert body["data"]["data"][0]["key"] == "FORBIDDEN_COUNTRIES"
    assert body["data"]["data"][0]["is_secret"] is False
    page = repos["settings"].search.call_args.args[0]
    assert (page.page, page.page_size) == (2, 10)
    assert repos["settings"].search.call_args.kwargs == {"key": "FORBIDDEN_COUNTRIES", "search": None}


def test_list_returns_secret_shaped_rows_with_their_value(api_client, as_super, repos):
    # user decision 2026-10-21 (deviation from TEOS-134 case 12): such rows exist only in the qa database
    repos["settings"].search.return_value = Paged(rows=[
        {"id": 1, "key": "SMTP_PASSWORD", "value": "hunter2", "created_at": None},
        {"id": 3, "key": "RESELLER_ID", "value": "r-1", "created_at": None},
    ], total=2)

    body = api_client.get(URL).json()["data"]

    assert [(r["key"], r["value"], r["is_secret"], r["editable"]) for r in body["data"]] == [
        ("SMTP_PASSWORD", "hunter2", False, True), ("RESELLER_ID", "r-1", False, True)]
    assert body["data"][1]["deletable"] is True  # RESELLER_ID is read from the environment, not protected
    assert body["total_count"] == 2


def test_list_rows_have_description_editable_and_deletable(api_client, as_super, repos):
    repos["settings"].search.return_value = Paged(rows=[
        {"id": 1, "key": "APP_CACHE_KEY", "value": "abc", "description": None, "created_at": None},
        {"id": 2, "key": "FORBIDDEN_COUNTRIES", "value": "IL", "description": "Hidden countries",
         "created_at": None},
        {"id": 3, "key": "MY_FLAG", "value": "1", "created_at": None},
    ], total=3)

    rows = api_client.get(URL).json()["data"]["data"]

    assert [(r["key"], r["editable"], r["deletable"]) for r in rows] == [
        ("APP_CACHE_KEY", False, False), ("FORBIDDEN_COUNTRIES", True, False), ("MY_FLAG", True, True)]
    assert rows[1]["description"] == "Hidden countries" and rows[2]["description"] is None


def test_list_search_by_part_of_the_key(api_client, as_super, repos):
    repos["settings"].search.return_value = Paged(rows=[], total=0)

    assert api_client.get(URL, params={"search": "countr"}).status_code == 200
    assert repos["settings"].search.call_args.kwargs == {"key": None, "search": "countr"}


def test_list_requires_a_session(api_client, repos):
    assert api_client.get(URL).status_code == 401


def test_list_is_super_admin_only(api_client, as_admin, repos):
    response = api_client.get(URL)
    assert response.status_code == 403
    assert response.json()["developerMessage"] == "admin.settings.read"


@pytest.mark.parametrize("params", [{"page_size": 101}, {"foo": "1"}, {"page": 0}])
def test_list_rejects_bad_params(api_client, as_super, repos, params):
    assert api_client.get(URL, params=params).status_code == 422


def test_list_database_error_is_502(api_client, as_super, repos):
    repos["settings"].search.side_effect = AdminUpstreamError("DATABASE_ERROR")
    assert api_client.get(URL).status_code == 502


# ---------------------------------------------------------------- PUT /settings

def _current(**values):
    return {key: {"id": i, "key": key, "value": value} for i, (key, value) in enumerate(values.items(), 1)}


def test_put_sends_only_changed_values_and_returns_the_result(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = _current(OTP_EXPIRATION_TIME="5", SUPPORT_EMAIL="a@x.com")
    repos["settings"].upsert_settings.return_value = {"updated": ["OTP_EXPIRATION_TIME"], "deleted": [],
                                                      "cache_key_rotated": False}

    response = api_client.put(URL, json={"items": [{"key": "OTP_EXPIRATION_TIME", "value": "7"},
                                                   {"key": "SUPPORT_EMAIL", "value": "a@x.com"}],
                                         "deleted_keys": []},
                              headers={"X-Forwarded-For": "8.8.8.8"})

    assert response.status_code == 200
    assert response.json()["data"] == {"updated": ["OTP_EXPIRATION_TIME"], "deleted": [], "cache_key_rotated": False}
    items, deleted, ctx = repos["settings"].upsert_settings.call_args.args
    assert items == [{"key": "OTP_EXPIRATION_TIME", "value": "7"}]
    assert deleted == []
    assert ctx.actor_id == ACTOR and ctx.ip == "8.8.8.8"


def test_put_new_key_and_delete(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = _current(OLD_FLAG="1")
    repos["settings"].upsert_settings.return_value = {"updated": ["NEW_FLAG"], "deleted": ["OLD_FLAG"],
                                                      "cache_key_rotated": False}

    response = api_client.put(URL, json={"items": [{"key": "NEW_FLAG", "value": "on"}],
                                         "deleted_keys": ["OLD_FLAG", "NOT_THERE"], "reason": "cleanup"})

    assert response.status_code == 200
    items, deleted, ctx = repos["settings"].upsert_settings.call_args.args
    assert items == [{"key": "NEW_FLAG", "value": "on"}]
    assert deleted == ["OLD_FLAG"]
    assert ctx.reason == "cleanup"


def test_put_forbidden_countries_reports_the_rotation(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = _current(FORBIDDEN_COUNTRIES="IL")
    repos["settings"].upsert_settings.return_value = {"updated": ["FORBIDDEN_COUNTRIES"], "deleted": [],
                                                      "cache_key_rotated": True}

    response = api_client.put(URL, json={"items": [{"key": "FORBIDDEN_COUNTRIES", "value": "IL,IR"}]})

    assert response.json()["data"]["cache_key_rotated"] is True


def test_put_nothing_changed_is_409_and_writes_nothing(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = _current(OTP_EXPIRATION_TIME="5")

    response = api_client.put(URL, json={"items": [{"key": "OTP_EXPIRATION_TIME", "value": "5"}],
                                         "deleted_keys": ["GONE_ALREADY"]})

    assert response.status_code == 409
    assert _code(response) == "no_change"
    repos["settings"].upsert_settings.assert_not_called()


def test_put_race_no_change_from_sql_is_409(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = {}
    repos["settings"].upsert_settings.side_effect = AdminConflict("no_change")

    response = api_client.put(URL, json={"items": [{"key": "NEW_FLAG", "value": "x"}]})

    assert response.status_code == 409 and _code(response) == "no_change"


@pytest.mark.parametrize("body, fragment", [
    ({"items": [{"key": "APP_CACHE_KEY", "value": "x"}]}, "cache_key_forbidden"),
    ({"items": [{"key": "TOP_COUNTRIES_COUNT", "value": "abc"}]}, "items.0.value: invalid_value"),
    ({"items": [{"key": "TOP_COUNTRIES_COUNT", "value": "0"}]}, "items.0.value: invalid_value"),
    ({"items": [{"key": "TOP_COUNTRIES_COUNT", "value": "51"}]}, "items.0.value: invalid_value"),
    ({"items": [{"key": "FORBIDDEN_COUNTRIES", "value": "IL,XX"}]}, "items.0.value: invalid_value"),
    ({"items": [{"key": "FORBIDDEN_COUNTRIES", "value": "IL, IR"}]}, "items.0.value: invalid_value"),
    ({"items": [{"key": "WALLET_TOP_UP_ALERT_RECIPIENTS", "value": "a@x.com,nope"}]},
     "items.0.value: invalid_value"),
    ({"items": [{"key": "VOUCHER_MAX_AMOUNT", "value": "-5"}]}, "items.0.value: invalid_value"),
    ({"items": [{"key": f"K{i:03d}", "value": "1"} for i in range(60)],
      "deleted_keys": [f"D{i:03d}" for i in range(41)]}, "too_many_items"),
    ({"items": [{"key": "A", "value": "1"}], "old_data": {}}, "old_data"),
    ({"items": [{"key": "A", "value": "1"}], "changed_by": "x"}, "changed_by"),
    ({"items": [{"key": "A", "value": "1"}, {"key": "A", "value": "2"}]}, "items.1.key: duplicate_key"),
    ({"items": [{"key": "A", "value": "1"}], "deleted_keys": ["A"]}, "also in items"),
    ({"items": [{"key": "HAS SPACE", "value": "1"}]}, "items.0.key"),
    ({"items": [{"key": "A", "value": "x" * 10_001}]}, "items.0.value"),
    ({"items": [], "deleted_keys": []}, "both empty"),
    ({}, "both empty"),
    ({"items": [{"key": "A", "value": "1"}], "extra": 1}, "extra"),
    ({"items": [{"key": "A", "value": None}]}, "items.0.value"),
])
def test_put_validation_is_422(api_client, as_super, repos, body, fragment):
    response = api_client.put(URL, json=body)

    assert response.status_code == 422
    assert fragment in response.json()["developerMessage"]
    repos["settings"].upsert_settings.assert_not_called()


def test_put_lowercase_new_key_is_422_but_existing_lowercase_key_is_editable(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = _current(login_type="email")
    repos["settings"].upsert_settings.return_value = {"updated": ["login_type"], "deleted": [],
                                                      "cache_key_rotated": False}

    bad = api_client.put(URL, json={"items": [{"key": "new_flag", "value": "1"}]})
    good = api_client.put(URL, json={"items": [{"key": "login_type", "value": "phone"}]})

    assert bad.status_code == 422 and "items.0.key: invalid_key" in bad.json()["developerMessage"]
    assert good.status_code == 200


def test_put_is_forbidden_for_admin(api_client, as_admin, repos):
    response = api_client.put(URL, json={"items": [{"key": "A", "value": "1"}]})
    assert response.status_code == 403


def test_put_requires_a_session(api_client, repos):
    assert api_client.put(URL, json={"items": [{"key": "A", "value": "1"}]}).status_code == 401


def test_put_database_error_is_502(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = {}
    repos["settings"].upsert_settings.side_effect = AdminUpstreamError("DATABASE_ERROR")
    assert api_client.put(URL, json={"items": [{"key": "NEW_FLAG", "value": "1"}]}).status_code == 502


# ---------------------------------------------------------------- TEOS-134 key rules

@pytest.mark.parametrize("deleted", [["APP_CACHE_KEY"], ["OLD_FLAG", "FORBIDDEN_COUNTRIES"],
                                     ["login_type"], ["VOUCHER_MAX_AMOUNT"]])
def test_put_deleting_a_protected_key_is_409_and_refuses_the_whole_batch(api_client, as_super, repos, deleted):
    repos["settings"].by_keys.return_value = _current(OLD_FLAG="1")

    response = api_client.put(URL, json={"items": [{"key": "NEW_FLAG", "value": "1"}], "deleted_keys": deleted})

    assert response.status_code == 409
    detail = json.loads(response.json()["developerMessage"])
    assert detail["code"] == "protected_key"
    assert detail["keys"] == [key for key in deleted if key != "OLD_FLAG"]
    repos["settings"].upsert_settings.assert_not_called()


@pytest.mark.parametrize("existing", [True, False])
def test_put_secret_shaped_keys_are_saved_like_any_other_key(api_client, as_super, repos, existing):
    # user decision 2026-10-21, for now: create, update and delete secret-shaped keys normally
    repos["settings"].by_keys.return_value = (
        _current(ESIM_HUB_API_KEY="old", RESELLER_ID="r-1") if existing else _current(RESELLER_ID="r-1"))
    repos["settings"].upsert_settings.return_value = {"updated": ["ESIM_HUB_API_KEY"], "deleted": ["RESELLER_ID"],
                                                      "cache_key_rotated": False}

    response = api_client.put(URL, json={"items": [{"key": "ESIM_HUB_API_KEY", "value": "new"}],
                                         "deleted_keys": ["RESELLER_ID"]})

    assert response.status_code == 200
    items, deleted, _ = repos["settings"].upsert_settings.call_args.args
    assert items == [{"key": "ESIM_HUB_API_KEY", "value": "new"}]
    assert deleted == ["RESELLER_ID"]


@pytest.mark.parametrize("key, value", [
    ("TOP_COUNTRIES_COUNT", "1"), ("TOP_COUNTRIES_COUNT", "50"), ("FORBIDDEN_COUNTRIES", "IL,IR,KP"),
    ("FORBIDDEN_COUNTRIES", ""), ("WALLET_TOP_UP_ALERT_RECIPIENTS", "a@x.com, ops@example.org"),
    ("VOUCHER_MAX_AMOUNT", "2500.50"),
])
def test_put_typed_values_are_accepted(api_client, as_super, repos, key, value):
    repos["settings"].by_keys.return_value = _current(**{key: "old"})
    repos["settings"].upsert_settings.return_value = {"updated": [key], "deleted": [], "cache_key_rotated": False}

    response = api_client.put(URL, json={"items": [{"key": key, "value": value}]})

    assert response.status_code == 200
    assert repos["settings"].upsert_settings.call_args.args[0] == [{"key": key, "value": value}]


@pytest.mark.parametrize("key", ["ABC_def", "AB", "A" * 65, "1ABC", "_ABC"])
def test_put_new_key_format_is_422(api_client, as_super, repos, key):
    repos["settings"].by_keys.return_value = {}

    response = api_client.put(URL, json={"items": [{"key": key, "value": "1"}]})

    assert response.status_code == 422
    repos["settings"].upsert_settings.assert_not_called()


def test_put_100_keys_is_accepted(api_client, as_super, repos):
    repos["settings"].by_keys.return_value = {}
    repos["settings"].upsert_settings.return_value = {"updated": [], "deleted": [], "cache_key_rotated": False}

    response = api_client.put(URL, json={"items": [{"key": f"KEY{i:03d}X", "value": "1"} for i in range(100)]})

    assert response.status_code == 200


@pytest.mark.parametrize("method", ["post", "delete", "patch"])
def test_other_methods_are_405(api_client, as_super, repos, method):
    assert getattr(api_client, method)(URL).status_code == 405


# ---------------------------------------------------------------- GET /settings/history

NEW_ROW = {
    "id": 9, "table_name": "app_config", "operation": "UPDATE", "changed_by": ACTOR,
    "changed_at": "2026-10-20T10:00:00", "record_id": None,
    "old_data": {"OTP_EXPIRATION_TIME": "5", "SMTP_PASSWORD": "old-secret"},
    "new_data": {"OTP_EXPIRATION_TIME": "7", "NEW_FLAG": "on"},
    "operation_details": {"ip_address": "8.8.8.8", "client_reported": False, "reason": None},
}
LEGACY_ROW = {
    "id": 3, "table_name": "app_config", "operation": "UPDATE", "changed_by": OTHER,
    "changed_at": "2025-01-01T10:00:00", "record_id": None,
    "old_data": json.dumps([{"id": 1, "key": "A", "value": "1", "created_at": "x"},
                            {"id": 2, "key": "B", "value": "2", "created_at": "x"}]),
    "new_data": json.dumps([{"key": "A", "value": "1"}, {"key": "B", "value": "3"}]),
    "operation_details": None,
}


def test_history_rows_have_changes_actor_and_redaction(api_client, as_super, repos):
    repos["audit"].search.return_value = AuditSearch(rows=[NEW_ROW, LEGACY_ROW], total=2)

    response = api_client.get(f"{URL}/history")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total_count"] == 2
    first, legacy = body["data"]
    assert first["changed_by"] == {"id": ACTOR, "email": "boss@example.com"}
    assert first["ip_address"] == "8.8.8.8" and first["client_reported"] is False
    assert first["changes"] == [
        {"key": "NEW_FLAG", "old_value": None, "new_value": "on", "redacted": False},
        {"key": "OTP_EXPIRATION_TIME", "old_value": "5", "new_value": "7", "redacted": False},
        {"key": "SMTP_PASSWORD", "old_value": "***", "new_value": None, "redacted": True},
    ]
    assert "old-secret" not in response.text
    assert legacy["changed_by"] is None  # actor deleted since
    assert legacy["client_reported"] is True
    assert legacy["changes"] == [{"key": "B", "old_value": "2", "new_value": "3", "redacted": False}]


def test_history_passes_filters_to_the_search(api_client, as_super, repos):
    repos["audit"].search.return_value = AuditSearch(rows=[], total=0)

    response = api_client.get(f"{URL}/history", params={"key": "FORBIDDEN_COUNTRIES", "changed_by": ACTOR,
                                                        "from": "2026-10-01", "to": "2026-10-20", "page": 3})

    assert response.status_code == 200
    assert response.json()["data"] == {"data": [], "total_count": 0, "facets": None, "totals": None}
    page = repos["audit"].search.call_args.args[0]
    kwargs = repos["audit"].search.call_args.kwargs
    assert page.page == 3
    assert kwargs["table"] == "app_config"
    assert kwargs["setting_key"] == "FORBIDDEN_COUNTRIES"
    assert kwargs["changed_by"] == ACTOR
    assert kwargs["changed_from"].isoformat() == "2026-10-01T00:00:00"
    assert kwargs["changed_to"].isoformat() == "2026-10-21T00:00:00"
    repos["users"].by_ids.assert_not_called()


@pytest.mark.parametrize("params", [
    {"from": "2026-10-21", "to": "2026-10-20"},
    {"changed_by": "not-a-uuid"},
    {"from": "yesterday"},
    {"page_size": 101},
    {"unknown": "1"},
])
def test_history_bad_params_are_422(api_client, as_super, repos, params):
    repos["audit"].search.return_value = AuditSearch(rows=[], total=0)
    assert api_client.get(f"{URL}/history", params=params).status_code == 422


def test_history_unparsable_row_has_parse_error(api_client, as_super, repos):
    broken = dict(LEGACY_ROW, id=4, old_data="{not json")
    repos["audit"].search.return_value = AuditSearch(rows=[broken], total=1)

    row = api_client.get(f"{URL}/history").json()["data"]["data"][0]

    assert row["changes"] == [] and row["parse_error"] is True


def test_history_detail(api_client, as_super, repos):
    repos["audit"].get.return_value = NEW_ROW

    response = api_client.get(f"{URL}/history/9")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == 9
    assert len(response.json()["data"]["changes"]) == 3
    repos["audit"].get.assert_called_once_with(9)


def test_history_detail_unknown_or_other_table_is_404(api_client, as_super, repos):
    repos["audit"].get.return_value = None
    assert api_client.get(f"{URL}/history/5").status_code == 404
    repos["audit"].get.return_value = dict(NEW_ROW, table_name="bundle")
    assert api_client.get(f"{URL}/history/9").status_code == 404


@pytest.mark.parametrize("bad_id", ["abc", "0"])
def test_history_detail_malformed_id_is_422(api_client, as_super, repos, bad_id):
    assert api_client.get(f"{URL}/history/{bad_id}").status_code == 422


def test_history_is_super_admin_only(api_client, as_admin, repos):
    assert api_client.get(f"{URL}/history").status_code == 403
    assert api_client.get(f"{URL}/history/1").status_code == 403


def test_history_requires_a_session(api_client, repos):
    assert api_client.get(f"{URL}/history").status_code == 401


def test_history_database_error_is_502(api_client, as_super, repos):
    repos["audit"].search.side_effect = AdminUpstreamError("DATABASE_ERROR")
    assert api_client.get(f"{URL}/history").status_code == 502


def test_openapi_documents_the_redaction_rule(api_client):
    spec = api_client.get("/openapi.json").json()
    text = json.dumps(spec["paths"]["/api/v1/admin/settings/history"])
    assert "SECRET|PASSWORD|TOKEN|PRIVATE|API_KEY" in text
