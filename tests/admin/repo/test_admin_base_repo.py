"""AdminBaseRepository: safe query building, paging, audited writes and error mapping.

Queries are built with the real PostgREST client (dummy URL from tests/conftest.py) and inspected
before execution; nothing is sent over the network.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.config.db import DatabaseTables
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError, AdminValidationError
from app.repo.admin.base import AdminBaseRepository, AuditContext
from app.schemas.admin.common import PageParams


class PromotionRepo(AdminBaseRepository):
    UNIQUE_CONFLICTS = {"promotion_code_key": "duplicate_code", "promotion_name_key": "duplicate_name"}

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION)


@pytest.fixture
def repo():
    return PromotionRepo()


def _api_error(code, message="boom", details=None):
    return APIError({"code": code, "message": message, "details": details, "hint": None})


# ---------- search and filters ----------

def test_like_escapes_wildcards_and_backslash():
    assert AdminBaseRepository.like("50%_off\\x") == "%50\\%\\_off\\\\x%"


def test_or_ilike_keeps_user_text_as_one_literal_value(repo):
    query = repo.or_ilike(repo.q(), ["name", "code"], ' a,is_active.eq.false%_"x) ')

    # every %, _ and \ is escaped for LIKE, then \ and " are escaped for the quoted PostgREST value
    expected = '"%a,is\\\\_active.eq.false\\\\%\\\\_\\"x)%"'
    assert query.params["or"] == f"(name.ilike.{expected},code.ilike.{expected})"


@pytest.mark.parametrize("term", [None, "", "   "])
def test_blank_search_adds_no_filter(repo, term):
    assert "or" not in repo.or_ilike(repo.q(), ["name"], term).params


def test_filters_quote_list_values_and_skip_none(repo):
    query = repo.filters(repo.q(), eq={"is_active": True, "type": None}, in_={"bundle_code": ['A,1', 'B"2']},
                         not_in={"id": [1, 2]}, gte={"valid_from": "2026-01-01"}, ilike={"code": "x_y"},
                         is_null={"deleted_at": True})

    params = dict(query.params.multi_items())
    assert params["is_active"] == "eq.True"
    assert "type" not in params
    assert params["bundle_code"] == 'in.("A,1","B\\"2")'
    assert params["id"] == 'not.in.("1","2")'
    assert params["valid_from"] == "gte.2026-01-01"
    assert params["code"] == "ilike.%x\\_y%"
    assert params["deleted_at"] == "is.null"


def test_empty_in_list_matches_nothing_and_empty_not_in_filters_nothing(repo):
    query = repo.filters(repo.q(), in_={"tag_id": []}, not_in={"id": []})

    params = dict(query.params.multi_items())
    assert params["tag_id"] == "in.()"
    assert "id" not in params


@pytest.mark.parametrize("column", ["name;drop", "Name", "a b", "x)", ""])
def test_column_names_are_validated(repo, column):
    with pytest.raises(ValueError):
        repo.filters(repo.q(), eq={column: 1})


# ---------- paging ----------

def test_paged_uses_a_stable_order_the_page_range_and_the_total(repo):
    query = MagicMock(name="query")
    query.order.return_value = query
    query.range.return_value.execute.return_value = SimpleNamespace(data=[{"id": 1}], count=42)

    page = repo.paged(query, PageParams(page=3, page_size=20))

    assert [c.args + (c.kwargs["desc"],) for c in query.order.call_args_list] == [
        ("created_at", True), ("id", True)]
    query.range.assert_called_once_with(40, 59)
    assert page.rows == [{"id": 1}]
    assert page.total == 42


def test_page_params_offsets():
    assert (PageParams(page=1, page_size=20).offset, PageParams(page=1, page_size=20).range_end) == (0, 19)


# ---------- audited writes ----------

def test_audited_writes_call_the_sql_functions_with_actor_ip_reason(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "rpc", lambda fn, params: calls.append((fn, params)) or {"id": 7})
    ctx = AuditContext(actor_id="admin-1", ip="1.2.3.4", reason="cleanup")

    repo.insert_audited({"code": "X"}, ctx)
    repo.update_audited(7, {"is_active": False}, ctx, rotate_cache=True, guard={"times_used": 0})
    repo.delete_audited(7, ctx, guard={"times_used": 0})

    assert calls[0] == ("admin_insert_audited", {"p_table": "promotion", "p_row": {"code": "X"},
                                                 "p_actor": "admin-1", "p_ip": "1.2.3.4", "p_reason": "cleanup",
                                                 "p_rotate_cache": False})
    assert calls[1] == ("admin_update_audited", {"p_table": "promotion", "p_id": "7",
                                                 "p_patch": {"is_active": False}, "p_actor": "admin-1",
                                                 "p_ip": "1.2.3.4", "p_reason": "cleanup", "p_operation": "UPDATE",
                                                 "p_rotate_cache": True, "p_guard": {"times_used": 0}})
    assert calls[2] == ("admin_delete_audited", {"p_table": "promotion", "p_id": "7", "p_actor": "admin-1",
                                                 "p_ip": "1.2.3.4", "p_reason": "cleanup", "p_rotate_cache": False,
                                                 "p_guard": {"times_used": 0}})


# ---------- error mapping ----------

def _raise(exc):
    def call():
        raise exc
    return call


@pytest.mark.parametrize("details, expected", [
    ('Key (code)=(X) already exists. promotion_code_key', "duplicate_code"),
    ('constraint "promotion_name_key"', "duplicate_name"),
    ("something else", "duplicate"),
])
def test_duplicate_maps_to_409_with_the_field_code(repo, details, expected):
    with pytest.raises(AdminConflict) as err:
        repo._run(_raise(_api_error("23505", "duplicate key value", details)))

    assert err.value.code == 409
    assert json.loads(err.value.details) == {"code": expected}


def test_foreign_key_violation_is_409_referenced(repo):
    with pytest.raises(AdminConflict) as err:
        repo._run(_raise(_api_error("23503")))
    assert json.loads(err.value.details) == {"code": "referenced"}


def test_sql_not_found_is_404(repo):
    with pytest.raises(AdminNotFound):
        repo._run(_raise(_api_error("AD404", "not_found")))


def test_sql_conflict_keeps_its_code_and_extra_fields(repo):
    with pytest.raises(AdminConflict) as err:
        repo._run(_raise(_api_error("AD409", "linked_bundles", '{"code": "linked_bundles", "bundle_count": 3}')))
    assert json.loads(err.value.details) == {"code": "linked_bundles", "bundle_count": 3}


@pytest.mark.parametrize("code", ["AD422", "22P02", "23502", "P0001"])
def test_invalid_input_is_422(repo, code):
    with pytest.raises(AdminValidationError):
        repo._run(_raise(_api_error(code, "Invalid bundle code: X")))


@pytest.mark.parametrize("exc", [_api_error("XX000", "internal"), ConnectionError("down"), TimeoutError()])
def test_other_failures_are_502_without_details(repo, exc):
    with pytest.raises(AdminUpstreamError) as err:
        repo._run(_raise(exc))
    assert err.value.details == "DATABASE_ERROR"
