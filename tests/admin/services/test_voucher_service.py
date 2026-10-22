"""VoucherAdminService and VoucherAdminRepo (MS-5089) with the database mocked."""
from datetime import date
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.base import AuditContext, Paged
from app.repo.admin.voucher_repo import VoucherAdminRepo
from app.schemas.admin.common import PageParams, AdminStatusPatch
from app.schemas.admin.vouchers import AdminVoucherCreate
from app.services.admin.voucher_service import VoucherAdminService

CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4", reason="r")


def _row(**extra):
    return {"id": 1, "code": "A1", "amount": 5.0, "is_used": False, "is_active": True, "used_by": None,
            "expired_at": None, "created_at": "2026-10-20T00:00:00", "updated_at": None, **extra}


@pytest.fixture
def repo():
    return MagicMock(spec=VoucherAdminRepo)


@pytest.fixture
def users():
    return MagicMock()


@pytest.fixture
def settings():
    mock = MagicMock()
    mock.value_of.return_value = None
    return mock


@pytest.fixture
def svc(repo, users, settings):
    return VoucherAdminService(repo=repo, users=users, settings=settings)


def test_list_joins_used_by_user_and_returns_facets(svc, repo, users):
    repo.search.return_value = Paged(rows=[_row(id=1, used_by="u1", is_used=True), _row(id=2),
                                           _row(id=3, used_by="gone")], total=3)
    repo.facets.return_value = {"all": 3, "active": 3, "inactive": 0}
    users.by_ids.return_value = {"u1": {"id": "u1", "email": "u1@example.com", "msisdn": "+1"}}
    page = PageParams(page=1, page_size=20)

    result = svc.list("a", "all", page)

    repo.search.assert_called_once_with("a", "all", page)
    repo.facets.assert_called_once_with("a")
    users.by_ids.assert_called_once_with(["u1", "gone"])
    assert result.total_count == 3
    assert result.facets == {"all": 3, "active": 3, "inactive": 0}
    assert result.data[0]["used_by_user"] == {"id": "u1", "email": "u1@example.com"}
    assert result.data[1]["used_by_user"] is None
    assert result.data[2]["used_by_user"] is None  # user deleted since
    assert result.data[0]["used_by"] == {"id": "u1", "email": "u1@example.com"}  # the object, not the raw uuid
    assert "msisdn" not in str(result.data[0])


def test_list_without_used_vouchers_skips_users_query(svc, repo, users):
    repo.search.return_value = Paged(rows=[_row()], total=1)
    repo.facets.return_value = {"all": 1, "active": 1, "inactive": 0}

    svc.list(None, "all", PageParams())

    users.by_ids.assert_not_called()


def test_create_inserts_audited_row(svc, repo):
    repo.insert_audited.return_value = _row(code="NEW1", amount=12.0, expired_at="2099-01-01T00:00:00")

    result = svc.create(AdminVoucherCreate(code="new1", amount=12, expired_at="2099-01-01"), CTX)

    repo.insert_audited.assert_called_once_with({"code": "NEW1", "amount": 12.0, "expired_at": "2099-01-01"}, CTX)
    assert result["code"] == "NEW1" and result["used_by_user"] is None


def test_create_accepts_a_timestamp_and_no_expiry(svc, repo):
    repo.insert_audited.return_value = _row()
    assert AdminVoucherCreate(code="ABCD", amount=1, expired_at="2099-01-01T00:00:00Z").expired_at == date(2099, 1, 1)

    svc.create(AdminVoucherCreate(code="ABCD", amount=1), CTX)

    assert repo.insert_audited.call_args.args[0]["expired_at"] is None


def test_set_status_updates_is_active(svc, repo, users):
    repo.update_audited.return_value = _row(is_active=False, used_by="u1")
    users.by_ids.return_value = {}

    result = svc.set_status(1, AdminStatusPatch(is_active=False), CTX)

    repo.update_audited.assert_called_once_with(1, {"is_active": False}, CTX)
    assert result["is_active"] is False


def test_set_status_no_change_propagates(svc, repo):
    repo.update_audited.side_effect = AdminConflict("no_change")
    with pytest.raises(AdminConflict) as err:
        svc.set_status(1, AdminStatusPatch(is_active=True), CTX)
    assert err.value.conflict_code == "no_change"


def test_delete_guards_on_is_used(svc, repo):
    svc.delete(1, CTX)
    repo.delete_audited.assert_called_once_with(1, CTX, guard={"is_used": False})


def test_delete_used_voucher_becomes_voucher_used(svc, repo):
    repo.delete_audited.side_effect = AdminConflict("guard_failed", expected={"is_used": False})
    with pytest.raises(AdminConflict) as err:
        svc.delete(1, CTX)
    assert err.value.conflict_code == "voucher_used"


def test_delete_missing_is_404(svc, repo):
    repo.delete_audited.side_effect = AdminNotFound("not_found")
    with pytest.raises(AdminNotFound):
        svc.delete(1, CTX)


# ---------- repository ----------

def _repo_with_client():
    repo = VoucherAdminRepo.__new__(VoucherAdminRepo)
    repo.table_name = "voucher"
    repo.client = MagicMock()
    return repo


@pytest.mark.parametrize("constraint", ["voucher_code_lower_key", "voucher_code_key"])
def test_repo_maps_unique_violation_to_duplicate_code(constraint):
    repo = _repo_with_client()
    error = APIError({"code": "23505", "message": f'duplicate key value violates unique constraint "{constraint}"',
                      "details": None, "hint": None})
    repo.client.rpc.return_value.execute.side_effect = error

    with pytest.raises(AdminConflict) as err:
        repo.insert_audited({"code": "A"}, CTX)
    assert err.value.conflict_code == "duplicate_code"


def test_repo_search_uses_literal_ilike_and_status():
    repo = VoucherAdminRepo.__new__(VoucherAdminRepo)
    query = MagicMock(name="query")
    repo.q = MagicMock(return_value=query)
    repo.filters = MagicMock(return_value=query)
    repo.paged = MagicMock(return_value=Paged(rows=[], total=0))

    repo.search("50%", "inactive", PageParams())

    repo.filters.assert_called_once_with(query, ilike={"code": "50%"}, eq={"is_active": False})


def test_repo_facets_count_over_the_search():
    repo = VoucherAdminRepo.__new__(VoucherAdminRepo)
    repo.count = MagicMock(side_effect=[5, 3, 1, 2])

    assert repo.facets("x") == {"all": 5, "active": 3, "inactive": 2, "used": 1, "expired": 2}
    calls = [call.kwargs for call in repo.count.call_args_list]
    assert calls[0] == {"ilike": {"code": "x"}}
    assert calls[1] == {"ilike": {"code": "x"}, "eq": {"is_active": True}}
    assert calls[2] == {"ilike": {"code": "x"}, "eq": {"is_used": True}}
    assert calls[3]["eq"] == {"is_used": False} and "expired_at" in calls[3]["lte"]


# ---------- TEOS-133 ----------

@pytest.mark.parametrize("status, expected", [
    ("all", {}),
    ("active", {"eq": {"is_active": True}}),
    ("inactive", {"eq": {"is_active": False}}),
    ("used", {"eq": {"is_used": True}}),
    ("expired", {"eq": {"is_used": False}, "lte": {"expired_at": "2026-10-21T00:00:00"}}),
])
def test_status_filters(status, expected):
    from app.repo.admin.voucher_repo import status_filters
    assert status_filters(status, now="2026-10-21T00:00:00") == expected


def test_repo_search_used_tab_filters_is_used():
    repo = VoucherAdminRepo.__new__(VoucherAdminRepo)
    query = MagicMock(name="query")
    repo.q = MagicMock(return_value=query)
    repo.filters = MagicMock(return_value=query)
    repo.paged = MagicMock(return_value=Paged(rows=[], total=0))

    repo.search(None, "used", PageParams())

    repo.filters.assert_called_once_with(query, ilike={"code": None}, eq={"is_used": True})


def test_list_rows_have_used_by_currency_and_is_expired(svc, repo, users, settings, monkeypatch):
    monkeypatch.setenv("DEFAULT_CURRENCY", "EUR")
    repo.search.return_value = Paged(rows=[_row(id=1, used_by="u1", is_used=True, expired_at="2000-01-01T00:00:00"),
                                           _row(id=2, expired_at="2000-01-01T00:00:00"),
                                           _row(id=3, expired_at="2999-01-01T00:00:00")], total=3)
    repo.facets.return_value = {"all": 3, "active": 3, "inactive": 0, "used": 1, "expired": 1}
    users.by_ids.return_value = {"u1": {"id": "u1", "email": "u1@example.com"}}

    result = svc.list(None, "all", PageParams())

    used, expired, valid = result.data
    assert used["used_by"] == used["used_by_user"] == {"id": "u1", "email": "u1@example.com"}
    assert [used["is_expired"], expired["is_expired"], valid["is_expired"]] == [False, True, False]
    assert used["currency"] == "EUR"
    assert result.facets["used"] == 1 and result.facets["expired"] == 1


def test_list_currency_comes_from_the_setting(svc, repo, settings):
    settings.value_of.return_value = "USD"
    repo.search.return_value = Paged(rows=[_row()], total=1)
    repo.facets.return_value = {}

    assert svc.list(None, "all", PageParams()).data[0]["currency"] == "USD"


def test_create_without_code_generates_a_12_char_code(svc, repo):
    from app.schemas.admin.vouchers import GENERATED_CODE_ALPHABET
    repo.insert_audited.side_effect = lambda row, ctx: _row(**row)

    result = svc.create(AdminVoucherCreate(amount=5), CTX)

    code = repo.insert_audited.call_args.args[0]["code"]
    assert len(code) == 12 and set(code) <= set(GENERATED_CODE_ALPHABET)
    assert not set(code) & set("0O1IL")
    assert result["code"] == code


def test_create_without_code_retries_on_a_conflict(svc, repo):
    repo.insert_audited.side_effect = [AdminConflict("duplicate_code"), AdminConflict("duplicate_code"),
                                       _row(code="ABCDEFGHJKMN")]

    assert svc.create(AdminVoucherCreate(amount=5), CTX)["code"] == "ABCDEFGHJKMN"
    assert repo.insert_audited.call_count == 3


def test_create_without_code_gives_up_after_the_attempts(svc, repo):
    from app.services.admin.voucher_service import GENERATE_ATTEMPTS
    repo.insert_audited.side_effect = AdminConflict("duplicate_code")

    with pytest.raises(AdminConflict) as err:
        svc.create(AdminVoucherCreate(amount=5), CTX)
    assert err.value.conflict_code == "duplicate_code"
    assert repo.insert_audited.call_count == GENERATE_ATTEMPTS


def test_create_with_explicit_code_does_not_retry(svc, repo):
    repo.insert_audited.side_effect = AdminConflict("duplicate_code")

    with pytest.raises(AdminConflict):
        svc.create(AdminVoucherCreate(code="SUMMER", amount=5), CTX)
    assert repo.insert_audited.call_count == 1


def test_generated_codes_do_not_collide():
    from app.services.admin.voucher_service import generate_code
    codes = {generate_code() for _ in range(100_000)}
    assert len(codes) == 100_000


@pytest.mark.parametrize("setting, amount, allowed", [
    (None, 1_000_000, True), (None, 1_000_001, False), ("500", 500, True), ("500", 500.01, False),
    ("not-a-number", 999_999, True), ("-3", 1_000_000, True), ("0", 1_000_001, False),
])
def test_create_amount_is_capped_by_the_setting(svc, repo, settings, setting, amount, allowed):
    settings.value_of.return_value = setting
    repo.insert_audited.return_value = _row()
    body = AdminVoucherCreate(code="ABCD", amount=amount)

    if allowed:
        svc.create(body, CTX)
        repo.insert_audited.assert_called_once()
    else:
        with pytest.raises(AdminValidationError) as err:
            svc.create(body, CTX)
        assert "VOUCHER_MAX_AMOUNT" in err.value.details
        repo.insert_audited.assert_not_called()
    settings.value_of.assert_any_call("VOUCHER_MAX_AMOUNT")


def test_create_amount_cap_falls_back_when_the_setting_read_fails(svc, repo, settings):
    settings.value_of.side_effect = RuntimeError("db down")
    repo.insert_audited.return_value = _row()

    svc.create(AdminVoucherCreate(code="ABCD", amount=1_000_000), CTX)

    repo.insert_audited.assert_called_once()


def test_activate_guards_on_is_used_and_maps_already_used(svc, repo):
    repo.update_audited.side_effect = AdminConflict("guard_failed", expected={"is_used": False})

    with pytest.raises(AdminConflict) as err:
        svc.set_status(1, AdminStatusPatch(is_active=True), CTX)

    assert err.value.conflict_code == "already_used"
    repo.update_audited.assert_called_once_with(1, {"is_active": True}, CTX, guard={"is_used": False})


def test_deactivate_a_used_voucher_is_allowed(svc, repo, users):
    repo.update_audited.return_value = _row(is_used=True, is_active=False)

    assert svc.set_status(1, AdminStatusPatch(is_active=False), CTX)["is_active"] is False
    repo.update_audited.assert_called_once_with(1, {"is_active": False}, CTX)


@pytest.mark.parametrize("code, ok", [("ABCD", True), ("summer-25", True), ("A" * 32, True), ("ABC", False),
                                      ("A" * 33, False), ("HAS SPACE", False), ("AB_CD", False), ("AB$CD", False)])
def test_code_pattern(code, ok):
    from pydantic import ValidationError
    if ok:
        assert AdminVoucherCreate(code=code, amount=1).code == code.upper()
    else:
        with pytest.raises(ValidationError):
            AdminVoucherCreate(code=code, amount=1)
