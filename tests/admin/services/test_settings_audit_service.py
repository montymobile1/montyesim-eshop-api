"""compute_changes, redaction, operation mapping and the repository calls of the settings / audit stories
(MS-5091, MS-5046, MS-5048). No network: repositories and the Supabase client are mocked."""
import json
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.app_config_repo import AppConfigAdminRepo
from app.repo.admin.audit_log_repo import AuditLogRepo, AuditSearch
from app.repo.admin.base import AuditContext
from app.schemas.admin.common import PageParams
from app.schemas.admin.settings import AdminSettingItem, AdminSettingsUpdate
from app.services.admin.audit_query_service import (AuditQueryService, client_reported, day_range,
                                                    is_secret_name, normalize_operation, parse_audit_json,
                                                    project_row, redact)
from app.services.admin.settings_service import SettingsAdminService, compute_changes, history_row

CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="8.8.8.8", reason=None)


# ---------------------------------------------------------------- compute_changes

def test_changes_lists_only_differing_keys():
    changes, error = compute_changes({"A": "1", "B": "2"}, {"A": "1", "B": "3"})
    assert error is False
    assert changes == [{"key": "B", "old_value": "2", "new_value": "3"}]


def test_created_and_deleted_keys():
    changes, _ = compute_changes({"GONE": "x"}, {"NEW": "y"})
    assert changes == [{"key": "GONE", "old_value": "x", "new_value": None},
                       {"key": "NEW", "old_value": None, "new_value": "y"}]


def test_legacy_string_lists_parse():
    old = json.dumps([{"id": 1, "key": "A", "value": "1", "created_at": "t"}, {"id": 2, "key": "B", "value": "2"}])
    new = json.dumps([{"key": "A", "value": "1"}, {"key": "C", "value": "3"}])
    changes, error = compute_changes(old, new)
    assert error is False
    assert [c["key"] for c in changes] == ["B", "C"]


def test_app_config_row_snapshot_shape():
    changes, _ = compute_changes({"id": 4, "key": "A", "value": "1", "created_at": "t"},
                                 {"id": 4, "key": "A", "value": "2", "created_at": "t"})
    assert changes == [{"key": "A", "old_value": "1", "new_value": "2"}]


@pytest.mark.parametrize("old", ["{broken", json.dumps([1, 2]), 42, json.dumps("text")])
def test_unparsable_data_gives_parse_error(old):
    assert compute_changes(old, {"A": "1"}) == ([], True)


def test_none_both_sides_is_no_change():
    assert compute_changes(None, None) == ([], False)


# ---------------------------------------------------------------- redaction

@pytest.mark.parametrize("name, secret", [
    ("SMTP_PASSWORD", True), ("STRIPE_SECRET_KEY", True), ("ESIM_HUB_API_KEY", True), ("SOME_TOKEN", True),
    ("PRIVATE_URL", True), ("RESELLER_ID", True), ("SMTP_USERNAME", True), ("fcm_token", True),
    ("APP_CACHE_KEY", False), ("CACHE_KEY", False), ("FORBIDDEN_COUNTRIES", False), ("key", False),
    (None, False), (3, False),
])
def test_is_secret_name(name, secret):
    assert is_secret_name(name) is secret


def test_redact_nested_and_key_value_items():
    data = {"A": "1", "nested": [{"key": "SMTP_PASSWORD", "value": "x"}, {"api_token": "t", "b": 2}],
            "DB_PASSWORD": None}
    assert redact(data) == {"A": "1", "nested": [{"key": "SMTP_PASSWORD", "value": "***"},
                                                 {"api_token": "***", "b": 2}], "DB_PASSWORD": None}


def test_history_row_redacts_secrets_in_changes():
    row = {"id": 1, "operation": "UPDATE", "changed_by": None, "old_data": {"SMTP_PASSWORD": "a"},
           "new_data": {"SMTP_PASSWORD": "b"}, "operation_details": {"client_reported": False}}
    result = history_row(row, {})
    assert result.changes[0].model_dump() == {"key": "SMTP_PASSWORD", "old_value": "***", "new_value": "***",
                                              "redacted": True}
    assert result.client_reported is False


# ---------------------------------------------------------------- operations and projection

@pytest.mark.parametrize("operation, details, expected", [
    ("UPDATE", {}, ("UPDATE", None)),
    ("insert", {}, ("INSERT", "insert")),
    ("Ban User", {}, ("BAN", "Ban User")),
    ("Unban User", {}, ("UNBAN", "Unban User")),
    ("Activate Bundle", {"rows_affected": 1}, ("UPDATE", "Activate Bundle")),
    ("Deactivate Bundle", {"rows_affected": 250}, ("BULK", "Deactivate Bundle")),
    ("EXPORT", {}, ("EXPORT", None)),
])
def test_normalize_operation(operation, details, expected):
    assert normalize_operation(operation, details) == expected


def test_client_reported_defaults_to_true_for_legacy_rows():
    assert client_reported({}) is True
    assert client_reported({"client_reported": False}) is False


def test_parse_audit_json():
    assert parse_audit_json('{"a": 1}') == ({"a": 1}, False)
    assert parse_audit_json({"a": 1}) == ({"a": 1}, False)
    assert parse_audit_json(None) == (None, False)
    assert parse_audit_json("{x") == (None, True)


def test_project_row_with_malformed_details_and_settings_summary():
    row = {"id": 1, "table_name": "app_config", "operation": "UPDATE", "changed_by": "u1",
           "old_data": {"A": "1"}, "new_data": {"A": "2", "B": "x"}, "operation_details": [1, 2]}
    projected = project_row(row, {"u1": {"id": "u1", "email": "a@x.com"}})
    assert projected["changed_by"].email == "a@x.com"
    assert projected["ip_address"] is None and projected["rows_affected"] is None
    assert projected["summary"] == "2 setting(s) changed: A, B"


def test_day_range():
    assert day_range(None, None) == (None, None)
    assert day_range(date(2026, 1, 1), date(2026, 1, 1)) == (datetime(2026, 1, 1), datetime(2026, 1, 2))
    with pytest.raises(AdminValidationError):
        day_range(date(2026, 1, 2), date(2026, 1, 1))


def test_audit_detail_404():
    audit = MagicMock()
    audit.get.return_value = None
    with pytest.raises(AdminNotFound):
        AuditQueryService(audit=audit, users=MagicMock()).get(5)


def test_audit_list_without_actors_does_not_query_users():
    audit, users = MagicMock(), MagicMock()
    audit.search.return_value = AuditSearch(rows=[], total=0)
    page = AuditQueryService(audit=audit, users=users).list(PageParams(page=9, page_size=20))
    assert page.total_count == 0 and page.data == [] and page.facets == {"by_table": {}}
    users.by_ids.assert_not_called()


# ---------------------------------------------------------------- settings service

def _service(current):
    settings = MagicMock()
    settings.by_keys.return_value = current
    settings.upsert_settings.return_value = [{"updated": ["A"], "deleted": [], "cache_key_rotated": False}]
    return SettingsAdminService(settings=settings, audit=MagicMock(), users=MagicMock()), settings


def test_update_unwraps_a_list_result_and_filters_unchanged_items():
    service, settings = _service({"A": {"key": "A", "value": "1"}, "B": {"key": "B", "value": "2"}})
    result = service.update(AdminSettingsUpdate(items=[AdminSettingItem(key="A", value="9"), AdminSettingItem(key="B", value="2")]),
                            CTX)
    assert result.updated == ["A"]
    assert settings.upsert_settings.call_args.args[0] == [{"key": "A", "value": "9"}]


def test_update_no_change():
    service, settings = _service({"A": {"key": "A", "value": "1"}})
    with pytest.raises(AdminConflict) as caught:
        service.update(AdminSettingsUpdate(items=[AdminSettingItem(key="A", value="1")]), CTX)
    assert caught.value.conflict_code == "no_change"
    settings.upsert_settings.assert_not_called()


def test_history_one_404_for_other_tables():
    service, _ = _service({})
    service.audit.get.return_value = {"id": 1, "table_name": "bundle"}
    with pytest.raises(AdminNotFound):
        service.history_one(1)


# ---------------------------------------------------------------- repositories (client mocked)

def _repo(cls):
    repo = cls.__new__(cls)
    repo.client = MagicMock()
    repo.table_name = "x"
    return repo


def test_audit_repo_search_calls_the_rpc():
    repo = _repo(AuditLogRepo)
    repo.client.rpc.return_value.execute.return_value.data = {
        "data": [{"id": 1}], "total": 5, "facets_by_table": {"bundle": 5}}

    result = repo.search(PageParams(page=2, page_size=10), table="bundle", operation="BULK",
                         changed_from=datetime(2026, 1, 1), setting_key=None)

    name, params = repo.client.rpc.call_args.args
    assert name == "admin_audit_log_search"
    assert params["p_limit"] == 10 and params["p_offset"] == 10
    assert params["p_table"] == "bundle" and params["p_operation"] == "BULK"
    assert params["p_from"] == "2026-01-01T00:00:00" and params["p_to"] is None
    assert result == AuditSearch(rows=[{"id": 1}], total=5, facets_by_table={"bundle": 5})


def test_app_config_repo_upsert_calls_the_rpc():
    repo = _repo(AppConfigAdminRepo)
    repo.client.rpc.return_value.execute.return_value.data = {"updated": [], "deleted": ["A"],
                                                              "cache_key_rotated": False}

    repo.upsert_settings([], ["A"], CTX)

    name, params = repo.client.rpc.call_args.args
    assert name == "admin_upsert_settings"
    assert params == {"p_items": [], "p_deleted": ["A"], "p_actor": CTX.actor_id, "p_ip": "8.8.8.8",
                      "p_reason": None}


# ---------------------------------------------------------------- TEOS-134 key rules

def test_update_refuses_a_protected_delete_before_reading_anything():
    service, settings = _service({"OLD": {"key": "OLD", "value": "1"}})
    with pytest.raises(AdminConflict) as caught:
        service.update(AdminSettingsUpdate(deleted_keys=["OLD", "FORBIDDEN_COUNTRIES", "APP_CACHE_KEY"]), CTX)
    assert caught.value.conflict_code == "protected_key"
    assert caught.value.extra == {"key": "FORBIDDEN_COUNTRIES", "keys": ["FORBIDDEN_COUNTRIES", "APP_CACHE_KEY"]}
    settings.by_keys.assert_not_called()
    settings.upsert_settings.assert_not_called()


def test_update_new_key_must_match_the_pattern():
    service, settings = _service({})
    with pytest.raises(AdminValidationError) as caught:
        service.update(AdminSettingsUpdate(items=[AdminSettingItem(key="AB", value="1")]), CTX)
    assert "items.0.key: invalid_key" in caught.value.details
    settings.upsert_settings.assert_not_called()


def test_list_marks_editable_and_deletable_and_passes_search():
    from app.repo.admin.base import Paged
    service, settings = _service({})
    settings.search.return_value = Paged(rows=[{"id": 1, "key": "MY_FLAG", "value": "1"},
                                               {"id": 2, "key": "TOP_COUNTRIES_COUNT", "value": "8"}], total=2)

    page = service.list(PageParams(), search="flag")

    settings.search.assert_called_once_with(PageParams(), key=None, search="flag")
    assert [(r.key, r.editable, r.deletable) for r in page.data] == [("MY_FLAG", True, True),
                                                                      ("TOP_COUNTRIES_COUNT", True, False)]


@pytest.mark.parametrize("key, value, ok", [
    ("TOP_COUNTRIES_COUNT", "10", True), ("TOP_COUNTRIES_COUNT", " 7 ", True), ("TOP_COUNTRIES_COUNT", "1.5", False),
    ("FORBIDDEN_COUNTRIES", "US", True), ("FORBIDDEN_COUNTRIES", "us", False), ("FORBIDDEN_COUNTRIES", "IL,IL", False),
    ("FORBIDDEN_COUNTRIES", "ISR", False), ("WALLET_TOP_UP_ALERT_RECIPIENTS", "", False),
    ("WALLET_TOP_UP_ALERT_RECIPIENTS", "a@x.com,", False), ("VOUCHER_MAX_AMOUNT", "abc", False),
    ("VOUCHER_MAX_AMOUNT", "0", False), ("VOUCHER_MAX_AMOUNT", "NaN", False), ("VOUCHER_MAX_AMOUNT", "1e3", True),
    ("ANY_OTHER", "whatever", True),
])
def test_setting_value_rules(key, value, ok):
    from app.schemas.admin.settings import setting_value_error
    assert (setting_value_error(key, value) is None) is ok


def test_protected_keys_cover_every_config_enum_key():
    from app.config.db import ConfigKeysEnum
    from app.schemas.admin.settings import PROTECTED_SETTING_KEYS
    assert {key.value for key in ConfigKeysEnum} <= PROTECTED_SETTING_KEYS
    assert {"APP_CACHE_KEY", "FORBIDDEN_COUNTRIES", "WALLET_TOP_UP_ALERT_RECIPIENTS"} <= PROTECTED_SETTING_KEYS
    # secrets are read from the environment (get_secret): not protected (user decision 2026-10-21)
    assert not {"RESELLER_ID", "ESIM_HUB_API_KEY"} & PROTECTED_SETTING_KEYS


def test_app_config_repo_search_selects_the_description_and_searches_the_key():
    repo = _repo(AppConfigAdminRepo)
    repo.table = MagicMock()
    query = repo.table.select.return_value
    query.eq.return_value = query
    query.ilike.return_value = query
    query.order.return_value = query
    query.range.return_value.execute.return_value = MagicMock(data=[], count=0)

    repo.search(PageParams(), key=None, search="countr")

    assert repo.table.select.call_args.args[0] == "id,key,value,description,created_at"
    query.or_.assert_not_called()  # secret-shaped rows are listed (user decision 2026-10-21)
    query.ilike.assert_called_once_with("key", "%countr%")
    query.eq.assert_not_called()


# ---------------------------------------------------------------- TEOS-218 typed values saved normalised

@pytest.mark.parametrize("key, sent, saved", [
    ("TOP_COUNTRIES_COUNT", " 10 ", "10"), ("TOP_COUNTRIES_COUNT", "007", "7"),
    ("WALLET_TOP_UP_ALERT_RECIPIENTS", " a@x.com , b@y.com ", "a@x.com,b@y.com"),
    ("VOUCHER_MAX_AMOUNT", " 250.5 ", "250.5"),
    ("FORBIDDEN_COUNTRIES", "US,IL", "US,IL"),
    ("ANY_OTHER", "  keep my spaces  ", "  keep my spaces  "),
])
def test_update_body_keeps_the_normalised_typed_value(key, sent, saved):
    body = AdminSettingsUpdate(items=[AdminSettingItem(key=key, value=sent)])
    assert body.items[0].value == saved


def test_update_saves_top_countries_count_trimmed():
    service, settings = _service({"TOP_COUNTRIES_COUNT": {"key": "TOP_COUNTRIES_COUNT", "value": "8"}})
    service.update(AdminSettingsUpdate(items=[AdminSettingItem(key="TOP_COUNTRIES_COUNT", value=" 10 ")]), CTX)
    assert settings.upsert_settings.call_args.args[0] == [{"key": "TOP_COUNTRIES_COUNT", "value": "10"}]


def test_update_saves_alert_recipients_trimmed():
    key = "WALLET_TOP_UP_ALERT_RECIPIENTS"
    service, settings = _service({key: {"key": key, "value": "old@x.com"}})
    service.update(AdminSettingsUpdate(items=[AdminSettingItem(key=key, value=" a@x.com ,  b@y.com")]), CTX)
    assert settings.upsert_settings.call_args.args[0] == [{"key": key, "value": "a@x.com,b@y.com"}]


def test_update_saves_an_untyped_value_exactly_as_sent():
    service, settings = _service({"MY_FLAG": {"key": "MY_FLAG", "value": "on"}})
    service.update(AdminSettingsUpdate(items=[AdminSettingItem(key="MY_FLAG", value=" on ")]), CTX)
    assert settings.upsert_settings.call_args.args[0] == [{"key": "MY_FLAG", "value": " on "}]


def test_update_padded_value_equal_to_the_stored_one_is_no_change():
    service, settings = _service({"TOP_COUNTRIES_COUNT": {"key": "TOP_COUNTRIES_COUNT", "value": "10"}})
    with pytest.raises(AdminConflict) as caught:
        service.update(AdminSettingsUpdate(items=[AdminSettingItem(key="TOP_COUNTRIES_COUNT", value=" 10 ")]), CTX)
    assert caught.value.conflict_code == "no_change"
    settings.upsert_settings.assert_not_called()
