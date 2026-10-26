"""PromotionAdminService and the promotion repositories (MS-5050, MS-5052, MS-5054) with the database mocked."""
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.base import AuditContext
from app.repo.admin.promotion_repo import PROMOTION_SELECT, PromotionAdminRepo, PromotionSearch
from app.repo.admin.promotion_rule_repo import PromotionRuleAdminRepo
from app.repo.admin.promotion_usage_repo import PromotionUsageAdminRepo, UsageSearch
from app.schemas.admin.common import AdminStatusPatch, PageParams
from app.schemas.admin.promotions import AdminPromotionCreate, AdminPromotionUpdate
from app.services.admin.promotion_service import (PromotionAdminService, day_range, is_amount_action,
                                                  join_bundle_codes, promotion_status, shape_promotion,
                                                  shape_totals, shape_usage_row, split_bundle_codes,
                                                  to_db_timestamp)

CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4", reason=None)
PROMO_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RULE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PCT_RULE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"

AMOUNT_RULE = {"id": RULE_ID, "name": "DISCOUNT_AMOUNT-CREATE_ORDER-3", "max_usage": 3, "beneficiary": 1,
               "promotion_rule_action": {"id": 1, "name": "DISCOUNT_AMOUNT"},
               "promotion_rule_event": {"id": 1, "name": "CREATE_ORDER"}}
PCT_RULE = {**AMOUNT_RULE, "id": PCT_RULE_ID, "name": "DISCOUNT_PERCENTAGE-CREATE_ORDER-3",
            "promotion_rule_action": {"id": 2, "name": "DISCOUNT_PERCENTAGE"}}


def _row(**extra):
    """A promotion as PostgREST returns it with the rule embed."""
    return {"id": PROMO_ID, "name": "Summer", "code": "SUMMER10", "type": "Promo", "amount": 10.0,
            "valid_from": "2026-10-01T00:00:00", "valid_to": "2099-12-31T00:00:00", "is_active": True,
            "times_used": 0, "created_at": "2026-10-20T00:00:00", "rule_id": RULE_ID,
            "bundle_code": "EU5, US1", "promotion_rule": AMOUNT_RULE, **extra}


def _create(**extra):
    return AdminPromotionCreate(**{"name": "Summer", "code": "SUMMER10", "rule_id": RULE_ID, "amount": 10,
                                   "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z",
                                   "bundle_codes": ["EU5", "US1"], **extra})


@pytest.fixture
def repo():
    return MagicMock(spec=PromotionAdminRepo)


@pytest.fixture
def rules():
    fake = MagicMock(spec=PromotionRuleAdminRepo)
    fake.get.side_effect = lambda rule_id: {RULE_ID: AMOUNT_RULE, PCT_RULE_ID: PCT_RULE}.get(str(rule_id))
    return fake


@pytest.fixture
def usage():
    return MagicMock(spec=PromotionUsageAdminRepo)


@pytest.fixture
def svc(repo, rules, usage):
    return PromotionAdminService(repo=repo, rules=rules, usage=usage)


# ---------------------------------------------------------------- helpers

def test_split_and_join_bundle_codes():
    assert split_bundle_codes("EU5, US1,,") == ["EU5", "US1"]
    assert split_bundle_codes(None) == [] and split_bundle_codes(["a", " "]) == ["a"]
    assert join_bundle_codes(["EU5", "US1"]) == "EU5,US1"
    assert join_bundle_codes([]) is None


def test_promotion_status_buckets():
    now = datetime(2026, 10, 21, 12, 0)
    assert promotion_status({"valid_to": "2026-10-20T00:00:00", "is_active": True}, now) == "expired"
    assert promotion_status({"valid_from": "2026-11-01T00:00:00", "is_active": False}, now) == "scheduled"
    assert promotion_status({"valid_from": "2026-10-01T00:00:00", "valid_to": "2026-12-01T00:00:00",
                             "is_active": True}, now) == "active"
    assert promotion_status({"is_active": False}, now) == "inactive"


def test_shape_promotion_from_embed_and_from_rpc_row():
    shaped = shape_promotion(_row())
    assert shaped["bundle_codes"] == ["EU5", "US1"]
    assert shaped["rule"]["action"] == {"id": 1, "name": "DISCOUNT_AMOUNT"}
    assert shaped["rule"]["event"]["name"] == "CREATE_ORDER"
    assert shaped["status"] == "active" and shaped["rule_id"] == RULE_ID
    assert "bundle_code" not in shaped and "callback_url" not in shaped

    rpc_row = {**_row(), "bundle_codes": ["EU5"], "status": "expired"}
    shaped = shape_promotion(rpc_row)
    assert shaped["bundle_codes"] == ["EU5"] and shaped["status"] == "expired"


def test_is_amount_action():
    assert is_amount_action(AMOUNT_RULE) is True
    assert is_amount_action(PCT_RULE) is False
    assert is_amount_action({"promotion_rule_action": {"id": 9, "name": "OTHER"}}) is None


def test_to_db_timestamp_is_utc_naive():
    assert to_db_timestamp(datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)) == "2026-10-01T02:00:00"
    assert to_db_timestamp(datetime.fromisoformat("2026-10-01T02:00:00+02:00")) == "2026-10-01T00:00:00"
    assert to_db_timestamp(None) is None


def test_day_range():
    start, end = day_range(date(2026, 1, 1), date(2026, 1, 31))
    assert (start.isoformat(), end.isoformat()) == ("2026-01-01T00:00:00", "2026-02-01T00:00:00")
    assert day_range(None, None) == (None, None)
    with pytest.raises(AdminValidationError):
        day_range(date(2026, 2, 1), date(2026, 1, 1))
    with pytest.raises(AdminValidationError):
        day_range(date(2025, 1, 1), date(2026, 1, 2))  # 367 days
    assert day_range(date(2025, 1, 1), date(2026, 1, 1))[0] is not None  # 366 days ok


def test_shape_usage_row_and_totals():
    row = shape_usage_row({"id": "u1", "created_at": "t", "promotion_code": "GONE", "promotion": None,
                           "user": {"id": "x", "email": "e", "msisdn": "+1"}, "bundle_id": None,
                           "bundle_code": None, "order_id": None, "discount_amount": 2.5, "currency": None,
                           "status": "completed"})
    assert row["promotion"] is None and row["user"]["email"] == "e" and row["promotion_code"] == "GONE"
    totals = shape_totals({"uses": "3", "distinct_users": 2,
                           "discount_amount": [{"currency": "USD", "amount": "7.50"}, {"currency": None, "amount": 1}]})
    assert totals == {"uses": 3, "distinct_users": 2,
                      "discount_amount": [{"currency": "USD", "amount": 7.5}, {"currency": None, "amount": 1.0}]}
    assert shape_totals({}) == {"uses": 0, "distinct_users": 0, "discount_amount": []}


# ---------------------------------------------------------------- list / get / usage

def test_list_calls_rpc_repo_and_fills_facets(svc, repo):
    repo.search.return_value = PromotionSearch(rows=[{**_row(), "bundle_codes": ["EU5"], "status": "active"}],
                                               total=1, facets={"all": 1, "active": 1})
    page = PageParams(page=1, page_size=20)

    result = svc.list(page, search="sum", status="active", rule_ids=[RULE_ID], bundle_codes=["EU5"],
                      include_global=True)

    repo.search.assert_called_once_with(page, search="sum", status="active", rule_ids=[RULE_ID],
                                        bundle_codes=["EU5"], include_global=True)
    assert result.facets == {"all": 1, "active": 1, "inactive": 0, "expired": 0, "scheduled": 0}
    assert result.data[0].bundle_codes == ["EU5"]


def test_get_shapes_or_404(svc, repo):
    repo.get.return_value = _row()
    assert svc.get(PROMO_ID)["code"] == "SUMMER10"
    repo.get.return_value = None
    with pytest.raises(AdminNotFound):
        svc.get(PROMO_ID)


def test_usage_passes_exclusive_end_and_totals(svc, usage):
    usage.search.return_value = UsageSearch(rows=[], total=0, totals={"uses": 0})
    page = PageParams()

    result = svc.usage(page, code="SUM", user_id="u", bundle_ids=["b"], date_from=date(2026, 1, 1),
                       date_to=date(2026, 1, 31))

    usage.search.assert_called_once_with(page, code="SUM", user_id="u", bundle_ids=["b"],
                                         created_from=datetime(2026, 1, 1), created_to=datetime(2026, 2, 1))
    assert result.totals == {"uses": 0, "distinct_users": 0, "discount_amount": []}


# ---------------------------------------------------------------- create

def test_create_builds_row_with_server_fields(svc, repo):
    repo.insert_audited.return_value = {"id": PROMO_ID}
    repo.get.return_value = _row()

    result = svc.create(_create(), CTX)

    row, ctx = repo.insert_audited.call_args.args
    assert row == {"name": "Summer", "code": "SUMMER10", "rule_id": RULE_ID, "amount": 10.0,
                   "valid_from": "2026-10-01T00:00:00", "valid_to": "2026-12-31T23:59:59",
                   "bundle_code": "EU5,US1", "type": "Promo", "is_active": True, "times_used": 0}
    assert ctx is CTX
    assert "callback_url" not in row and "callback_headers" not in row
    assert result["id"] == PROMO_ID


def test_create_percentage_without_amount_and_no_bundles(svc, repo):
    repo.insert_audited.return_value = {"id": PROMO_ID}
    repo.get.return_value = _row(amount=None, bundle_code=None, promotion_rule=PCT_RULE)

    result = svc.create(_create(rule_id=PCT_RULE_ID, amount=None, bundle_codes=[]), CTX)

    row = repo.insert_audited.call_args.args[0]
    assert row["amount"] is None and row["bundle_code"] is None
    assert result["bundle_codes"] == []


def test_create_amount_rule_without_amount_is_422(svc, repo):
    with pytest.raises(AdminValidationError):
        svc.create(_create(amount=None), CTX)
    repo.insert_audited.assert_not_called()


def test_create_percentage_over_100_is_422(svc, repo):
    with pytest.raises(AdminValidationError):
        svc.create(_create(rule_id=PCT_RULE_ID, amount=150), CTX)
    repo.insert_audited.assert_not_called()


def test_create_unknown_rule_is_422(svc, repo):
    with pytest.raises(AdminValidationError):
        svc.create(_create(rule_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaff"), CTX)
    repo.insert_audited.assert_not_called()


def test_create_unknown_bundle_code_from_trigger_is_422(svc, repo):
    repo.insert_audited.side_effect = AdminValidationError("Invalid bundle code: XX")
    with pytest.raises(AdminValidationError) as err:
        svc.create(_create(), CTX)
    assert "XX" in str(err.value.details)


@pytest.mark.parametrize("code", ["duplicate_code", "duplicate_name"])
def test_create_duplicate_propagates(svc, repo, code):
    repo.insert_audited.side_effect = AdminConflict(code)
    with pytest.raises(AdminConflict) as err:
        svc.create(_create(), CTX)
    assert err.value.conflict_code == code


def test_create_schema_rejects_bad_dates_and_codes():
    with pytest.raises(ValueError):
        _create(valid_to="2026-09-01T00:00:00Z")
    with pytest.raises(ValueError):
        _create(bundle_codes=["A", "B", "C", "D", "E", "F"])
    with pytest.raises(ValueError):
        _create(bundle_codes=["A,B"])
    assert _create(bundle_codes=[" EU5 ", "EU5", "US1"]).bundle_codes == ["EU5", "US1"]


# ---------------------------------------------------------------- update

def test_update_editable_fields_only_no_guard(svc, repo):
    repo.get.return_value = _row()
    repo.update_audited.return_value = _row(name="Winter")

    svc.update(PROMO_ID, AdminPromotionUpdate(name="Winter", bundle_codes=["EU5"],
                                              valid_to="2027-01-31T00:00:00Z"), CTX)

    repo.update_audited.assert_called_once_with(
        PROMO_ID, {"name": "Winter", "bundle_code": "EU5", "valid_to": "2027-01-31T00:00:00"}, CTX, guard=None)


@pytest.mark.parametrize("extra", [{}, {"bundle_codes": None}], ids=["missing", "null"])
def test_update_without_bundle_codes_keeps_the_bundles(svc, repo, extra):
    """TEOS-226: renaming or extending a promotion must not make it apply to every bundle."""
    repo.get.return_value = _row()
    repo.update_audited.return_value = _row(name="Winter")

    svc.update(PROMO_ID, AdminPromotionUpdate(name="Winter", valid_to="2027-01-31T00:00:00Z", **extra), CTX)

    patch = repo.update_audited.call_args.args[1]
    assert "bundle_code" not in patch
    assert patch == {"name": "Winter", "valid_to": "2027-01-31T00:00:00"}


def test_update_explicit_empty_bundle_codes_applies_to_every_bundle(svc, repo):
    repo.get.return_value = _row()
    repo.update_audited.return_value = _row(bundle_code=None)

    svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=[]), CTX)

    assert repo.update_audited.call_args.args[1] == {"name": "Summer", "bundle_code": None}


def test_update_unchanged_immutable_values_are_not_a_change(svc, repo):
    repo.get.return_value = _row()
    repo.update_audited.return_value = _row()

    svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=["EU5", "US1"], code="SUMMER10",
                                              rule_id=RULE_ID, amount=10, valid_from="2026-10-01T00:00:00Z"), CTX)

    patch, guard = repo.update_audited.call_args.args[1], repo.update_audited.call_args.kwargs["guard"]
    assert patch == {"name": "Summer", "bundle_code": "EU5,US1"} and guard is None


def test_update_immutable_change_on_unused_promotion_is_guarded(svc, repo):
    repo.get.return_value = _row(times_used=0)
    repo.update_audited.return_value = _row(code="NEW1")

    svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=[], code="NEW1", amount=25), CTX)

    patch = repo.update_audited.call_args.args[1]
    assert patch["code"] == "NEW1" and patch["amount"] == 25.0 and patch["bundle_code"] is None
    assert repo.update_audited.call_args.kwargs["guard"] == {"times_used": 0}


def test_update_immutable_change_on_used_promotion_is_409(svc, repo):
    repo.get.return_value = _row(times_used=4)

    with pytest.raises(AdminConflict) as err:
        svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=[], code="NEW1"), CTX)

    assert err.value.conflict_code == "promotion_used" and err.value.extra == {"times_used": 4, "fields": ["code"]}
    repo.update_audited.assert_not_called()


def test_update_name_of_used_promotion_is_allowed(svc, repo):
    repo.get.return_value = _row(times_used=4)
    repo.update_audited.return_value = _row(times_used=4, name="Winter")

    assert svc.update(PROMO_ID, AdminPromotionUpdate(name="Winter", bundle_codes=["EU5", "US1"]), CTX)["name"]
    repo.update_audited.assert_called_once()


def test_update_guard_failed_becomes_promotion_used(svc, repo):
    repo.get.return_value = _row(times_used=0)
    repo.update_audited.side_effect = AdminConflict("guard_failed", expected={"times_used": 0})
    with pytest.raises(AdminConflict) as err:
        svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=[], code="NEW1"), CTX)
    assert err.value.conflict_code == "promotion_used"


def test_update_no_change_returns_current_row(svc, repo):
    repo.get.return_value = _row()
    repo.update_audited.side_effect = AdminConflict("no_change")
    assert svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=["EU5", "US1"]), CTX)["id"] == PROMO_ID


def test_update_rule_change_checks_amount_rule(svc, repo):
    repo.get.return_value = _row(times_used=0)
    with pytest.raises(AdminValidationError):  # percentage rule with amount 10 (current) is fine, 150 is not
        svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=[], rule_id=PCT_RULE_ID, amount=150), CTX)
    repo.update_audited.assert_not_called()


def test_update_unknown_is_404(svc, repo):
    repo.get.return_value = None
    with pytest.raises(AdminNotFound):
        svc.update(PROMO_ID, AdminPromotionUpdate(name="Summer", bundle_codes=[]), CTX)


# ---------------------------------------------------------------- status / delete

def test_set_status_updates_is_active_then_rereads(svc, repo):
    repo.update_audited.return_value = _row(is_active=False)
    repo.get.return_value = _row(is_active=False)

    result = svc.set_status(PROMO_ID, AdminStatusPatch(is_active=False), CTX)

    repo.update_audited.assert_called_once_with(PROMO_ID, {"is_active": False}, CTX)
    assert result["is_active"] is False


def test_set_status_no_change_propagates(svc, repo):
    repo.update_audited.side_effect = AdminConflict("no_change")
    with pytest.raises(AdminConflict) as err:
        svc.set_status(PROMO_ID, AdminStatusPatch(is_active=True), CTX)
    assert err.value.conflict_code == "no_change"


def test_delete_unused_promotion_is_guarded(svc, repo):
    repo.get.return_value = _row(times_used=0)
    svc.delete(PROMO_ID, CTX)
    repo.delete_audited.assert_called_once_with(PROMO_ID, CTX, guard={"times_used": 0})


def test_delete_used_promotion_is_409_with_times_used(svc, repo):
    repo.get.return_value = _row(times_used=3)
    with pytest.raises(AdminConflict) as err:
        svc.delete(PROMO_ID, CTX)
    assert err.value.conflict_code == "promotion_used" and err.value.extra == {"times_used": 3}
    repo.delete_audited.assert_not_called()


@pytest.mark.parametrize("db_code", ["referenced", "guard_failed"])
def test_delete_refused_by_database_is_promotion_used(svc, repo, db_code):
    repo.get.return_value = _row(times_used=0)
    repo.delete_audited.side_effect = AdminConflict(db_code)
    with pytest.raises(AdminConflict) as err:
        svc.delete(PROMO_ID, CTX)
    assert err.value.conflict_code == "promotion_used" and err.value.extra["times_used"] == 1


def test_delete_missing_is_404(svc, repo):
    repo.get.return_value = None
    with pytest.raises(AdminNotFound):
        svc.delete(PROMO_ID, CTX)


# ---------------------------------------------------------------- repositories

def _promotion_repo():
    repo = PromotionAdminRepo.__new__(PromotionAdminRepo)
    repo.table_name = "promotion"
    repo.client = MagicMock()
    return repo


@pytest.mark.parametrize("constraint, code", [("promotion_code_key", "duplicate_code"),
                                              ("promotion_name_key", "duplicate_name")])
def test_promotion_repo_maps_unique_violations(constraint, code):
    repo = _promotion_repo()
    error = APIError({"code": "23505", "message": f'duplicate key value violates unique constraint "{constraint}"',
                      "details": None, "hint": None})
    repo.client.rpc.return_value.execute.side_effect = error
    with pytest.raises(AdminConflict) as err:
        repo.insert_audited({"code": "A"}, CTX)
    assert err.value.conflict_code == code


def test_promotion_repo_trigger_error_is_422():
    repo = _promotion_repo()
    error = APIError({"code": "P0001", "message": "Invalid bundle code: XX", "details": None, "hint": None})
    repo.client.rpc.return_value.execute.side_effect = error
    with pytest.raises(AdminValidationError) as err:
        repo.insert_audited({"code": "A"}, CTX)
    assert "Invalid bundle code: XX" in str(err.value.details)


def test_promotion_repo_search_calls_rpc_with_bound_params():
    repo = _promotion_repo()
    repo.client.rpc.return_value.execute.return_value = MagicMock(
        data={"data": [{"id": PROMO_ID}], "total": 1, "facets": {"all": 1, "active": "1"}})

    result = repo.search(PageParams(page=2, page_size=10), search="a,code.ilike.%", status="active",
                         rule_ids=[RULE_ID], bundle_codes=["EU)5"], include_global=True)

    repo.client.rpc.assert_called_once_with("admin_search_promotions", {
        "p_search": "a,code.ilike.%", "p_status": "active", "p_rule_ids": [RULE_ID], "p_bundle_codes": ["EU)5"],
        "p_include_global": True, "p_limit": 10, "p_offset": 10})
    assert result.total == 1 and result.facets == {"all": 1, "active": 1}


def test_promotion_repo_search_accepts_list_wrapped_result_and_empty_filters():
    repo = _promotion_repo()
    repo.client.rpc.return_value.execute.return_value = MagicMock(data=[{"data": [], "total": 0, "facets": {}}])

    result = repo.search(PageParams(), rule_ids=[], bundle_codes=[])

    params = repo.client.rpc.call_args.args[1]
    assert params["p_rule_ids"] is None and params["p_bundle_codes"] is None
    assert result.rows == [] and result.total == 0


def test_promotion_repo_get_selects_rule_embed_without_callbacks():
    repo = PromotionAdminRepo.__new__(PromotionAdminRepo)
    repo.one = MagicMock(return_value=_row())
    repo.get(PROMO_ID)
    repo.one.assert_called_once_with(PROMO_ID, PROMOTION_SELECT)
    assert "callback" not in PROMOTION_SELECT


def test_usage_repo_calls_rpc():
    repo = PromotionUsageAdminRepo.__new__(PromotionUsageAdminRepo)
    repo.table_name = "promotion_usage"
    repo.client = MagicMock()
    repo.client.rpc.return_value.execute.return_value = MagicMock(
        data={"data": [{"id": "u1"}], "total": 1, "totals": {"uses": 1}})

    result = repo.search(PageParams(page=1, page_size=50), code="SUM", user_id="u", bundle_ids=["b1"],
                         created_from=datetime(2026, 1, 1), created_to=datetime(2026, 2, 1))

    repo.client.rpc.assert_called_once_with("admin_promotion_usage", {
        "p_code": "SUM", "p_user_id": "u", "p_bundle_ids": ["b1"], "p_from": "2026-01-01T00:00:00",
        "p_to": "2026-02-01T00:00:00", "p_limit": 50, "p_offset": 0})
    assert result.total == 1 and result.totals == {"uses": 1}


def test_usage_repo_database_error_is_502():
    from app.exceptions.admin import AdminUpstreamError
    repo = PromotionUsageAdminRepo.__new__(PromotionUsageAdminRepo)
    repo.table_name = "promotion_usage"
    repo.client = MagicMock()
    repo.client.rpc.return_value.execute.side_effect = APIError({"code": "42883", "message": "function missing",
                                                                 "details": None, "hint": None})
    with pytest.raises(AdminUpstreamError):
        repo.search(PageParams())
