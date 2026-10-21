"""VoucherAdminService and VoucherAdminRepo (MS-5089) with the database mocked."""
from datetime import date
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminConflict, AdminNotFound
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
def svc(repo, users):
    return VoucherAdminService(repo=repo, users=users)


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
    assert "used_by" not in result.data[0] and "msisdn" not in str(result.data[0])


def test_list_without_used_vouchers_skips_users_query(svc, repo, users):
    repo.search.return_value = Paged(rows=[_row()], total=1)
    repo.facets.return_value = {"all": 1, "active": 1, "inactive": 0}

    svc.list(None, "all", PageParams())

    users.by_ids.assert_not_called()


def test_create_inserts_audited_row(svc, repo):
    repo.insert_audited.return_value = _row(code="NEW", amount=12.0, expired_at="2099-01-01T00:00:00")

    result = svc.create(AdminVoucherCreate(code="NEW", amount=12, expired_at="2099-01-01"), CTX)

    repo.insert_audited.assert_called_once_with({"code": "NEW", "amount": 12.0, "expired_at": "2099-01-01"}, CTX)
    assert result["code"] == "NEW" and result["used_by_user"] is None


def test_create_accepts_a_timestamp_and_no_expiry(svc, repo):
    repo.insert_audited.return_value = _row()
    assert AdminVoucherCreate(code="AB", amount=1, expired_at="2099-01-01T00:00:00Z").expired_at == date(2099, 1, 1)

    svc.create(AdminVoucherCreate(code="AB", amount=1), CTX)

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
    repo.count = MagicMock(side_effect=[5, 3])

    assert repo.facets("x") == {"all": 5, "active": 3, "inactive": 2}
    assert repo.count.call_args_list[0].kwargs == {"ilike": {"code": "x"}}
    assert repo.count.call_args_list[1].kwargs == {"ilike": {"code": "x"}, "eq": {"is_active": True}}
