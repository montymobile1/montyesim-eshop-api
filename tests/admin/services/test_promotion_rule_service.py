"""PromotionRuleAdminService and PromotionRuleAdminRepo (MS-5087) with the database mocked."""
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.base import AuditContext, Paged
from app.repo.admin.promotion_rule_repo import (RULE_SELECT, PromotionRuleActionAdminRepo, PromotionRuleAdminRepo,
                                                PromotionRuleEventAdminRepo)
from app.schemas.admin.common import PageParams
from app.schemas.admin.promotion_rules import AdminPromotionRuleBody
from app.services.admin.promotion_rule_service import PromotionRuleAdminService, shape_rule

CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4", reason=None)
RULE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACTIONS = [{"id": 1, "name": "DISCOUNT_AMOUNT"}, {"id": 2, "name": "DISCOUNT_PERCENTAGE"}]
EVENTS = [{"id": 1, "name": "CREATE_ORDER"}, {"id": 2, "name": "CREATE_ACCOUNT"}]


def _row(**extra):
    return {"id": RULE_ID, "id_text": RULE_ID, "promotion_rule_action_id": 1, "promotion_rule_event_id": 1,
            "max_usage": 3, "beneficiary": 1, "name": "DISCOUNT_AMOUNT-CREATE_ORDER-3",
            "created_at": "2026-10-20T00:00:00",
            "promotion_rule_action": {"id": 1, "name": "DISCOUNT_AMOUNT"},
            "promotion_rule_event": {"id": 1, "name": "CREATE_ORDER"}, **extra}


def _body(**extra):
    return AdminPromotionRuleBody(**{"promotion_rule_action_id": 1, "promotion_rule_event_id": 1, "max_usage": 3,
                                     "beneficiary": 1, **extra})


@pytest.fixture
def repo():
    return MagicMock(spec=PromotionRuleAdminRepo)


@pytest.fixture
def actions():
    fake = MagicMock(spec=PromotionRuleActionAdminRepo)
    fake.all.return_value = ACTIONS
    return fake


@pytest.fixture
def events():
    fake = MagicMock(spec=PromotionRuleEventAdminRepo)
    fake.all.return_value = EVENTS
    return fake


@pytest.fixture
def svc(repo, actions, events):
    return PromotionRuleAdminService(repo=repo, actions=actions, events=events)


# ---------- shaping ----------

def test_shape_rule_projects_embeds():
    shaped = shape_rule(_row())
    assert shaped["action"] == {"id": 1, "name": "DISCOUNT_AMOUNT"}
    assert shaped["event"] == {"id": 1, "name": "CREATE_ORDER"}
    assert shaped["id_text"] == RULE_ID and shaped["max_usage"] == 3
    assert "promotion_rule_action_id" not in shaped


def test_shape_rule_without_embeds_keeps_ids():
    shaped = shape_rule({"id": RULE_ID, "promotion_rule_action_id": 2, "promotion_rule_event_id": 1, "max_usage": 1})
    assert shaped["action"] == {"id": 2, "name": None}
    assert shaped["id_text"] == RULE_ID


# ---------- reads ----------

def test_list_passes_filters_and_shapes(svc, repo):
    repo.search.return_value = Paged(rows=[_row()], total=1)
    page = PageParams(page=2, page_size=5)

    result = svc.list("aaa", 1, 2, page)

    repo.search.assert_called_once_with("aaa", 1, 2, page)
    assert result.total_count == 1 and result.data[0]["action"]["name"] == "DISCOUNT_AMOUNT"


def test_actions_and_events(svc):
    assert svc.actions() == ACTIONS
    assert svc.events() == EVENTS


def test_get_unknown_is_404(svc, repo):
    repo.get.return_value = None
    with pytest.raises(AdminNotFound):
        svc.get(RULE_ID)


# ---------- writes ----------

def test_create_inserts_audited_then_rereads(svc, repo):
    repo.insert_audited.return_value = {"id": RULE_ID}
    repo.get.return_value = _row()

    result = svc.create(_body(), CTX)

    repo.insert_audited.assert_called_once_with(
        {"promotion_rule_action_id": 1, "promotion_rule_event_id": 1, "max_usage": 3, "beneficiary": 1}, CTX)
    repo.get.assert_called_once_with(RULE_ID)
    assert result["name"] == "DISCOUNT_AMOUNT-CREATE_ORDER-3"


def test_create_unknown_action_or_event_is_422_before_writing(svc, repo):
    with pytest.raises(AdminValidationError):
        svc.create(_body(promotion_rule_action_id=9), CTX)
    with pytest.raises(AdminValidationError):
        svc.create(_body(promotion_rule_event_id=9), CTX)
    repo.insert_audited.assert_not_called()


def test_create_duplicate_propagates(svc, repo):
    repo.insert_audited.side_effect = AdminConflict("duplicate_rule")
    with pytest.raises(AdminConflict) as err:
        svc.create(_body(), CTX)
    assert err.value.conflict_code == "duplicate_rule"


def test_create_fk_failure_becomes_422(svc, repo):
    repo.insert_audited.side_effect = AdminConflict("referenced")
    with pytest.raises(AdminValidationError):
        svc.create(_body(), CTX)


def test_update_writes_patch_and_rereads(svc, repo):
    repo.update_audited.return_value = _row(beneficiary=2)
    repo.get.return_value = _row(beneficiary=2)

    result = svc.update(RULE_ID, _body(beneficiary=2), CTX)

    repo.update_audited.assert_called_once_with(
        RULE_ID, {"promotion_rule_action_id": 1, "promotion_rule_event_id": 1, "max_usage": 3, "beneficiary": 2}, CTX)
    assert result["beneficiary"] == 2


def test_update_without_change_returns_current_row(svc, repo):
    repo.update_audited.side_effect = AdminConflict("no_change")
    repo.get.return_value = _row()
    assert svc.update(RULE_ID, _body(), CTX)["id"] == RULE_ID


def test_update_duplicate_and_missing_propagate(svc, repo):
    repo.update_audited.side_effect = AdminConflict("duplicate_rule")
    with pytest.raises(AdminConflict) as err:
        svc.update(RULE_ID, _body(), CTX)
    assert err.value.conflict_code == "duplicate_rule"

    repo.update_audited.side_effect = AdminNotFound("not_found")
    with pytest.raises(AdminNotFound):
        svc.update(RULE_ID, _body(), CTX)


def test_delete_audited(svc, repo):
    svc.delete(RULE_ID, CTX)
    repo.delete_audited.assert_called_once_with(RULE_ID, CTX)


def test_delete_referenced_becomes_rule_in_use(svc, repo):
    repo.delete_audited.side_effect = AdminConflict("referenced")
    with pytest.raises(AdminConflict) as err:
        svc.delete(RULE_ID, CTX)
    assert err.value.conflict_code == "rule_in_use"


def test_delete_missing_is_404(svc, repo):
    repo.delete_audited.side_effect = AdminNotFound("not_found")
    with pytest.raises(AdminNotFound):
        svc.delete(RULE_ID, CTX)


# ---------- repository ----------

def _repo_with_client():
    repo = PromotionRuleAdminRepo.__new__(PromotionRuleAdminRepo)
    repo.table_name = "promotion_rule"
    repo.client = MagicMock()
    return repo


def test_repo_maps_unique_violation_to_duplicate_rule():
    repo = _repo_with_client()
    error = APIError({"code": "23505", "message": 'duplicate key value violates unique constraint '
                                                  '"promotion_rule_action_event_max_usage_key"',
                      "details": None, "hint": None})
    repo.client.rpc.return_value.execute.side_effect = error

    with pytest.raises(AdminConflict) as err:
        repo.insert_audited({"max_usage": 1}, CTX)
    assert err.value.conflict_code == "duplicate_rule"


def test_repo_maps_fk_violation_to_referenced():
    repo = _repo_with_client()
    error = APIError({"code": "23503", "message": "violates foreign key constraint promotion_rule_id_fkey",
                      "details": None, "hint": None})
    repo.client.rpc.return_value.execute.side_effect = error

    with pytest.raises(AdminConflict) as err:
        repo.delete_audited(RULE_ID, CTX)
    assert err.value.conflict_code == "referenced"


def test_repo_search_uses_literal_ilike_on_id_text_and_eq_filters():
    repo = PromotionRuleAdminRepo.__new__(PromotionRuleAdminRepo)
    query = MagicMock(name="query")
    repo.q = MagicMock(return_value=query)
    repo.filters = MagicMock(return_value=query)
    repo.paged = MagicMock(return_value=Paged(rows=[], total=0))
    page = PageParams()

    repo.search("ab%", 1, None, page)

    repo.q.assert_called_once_with(RULE_SELECT)
    repo.filters.assert_called_once_with(query, ilike={"id_text": "ab%"},
                                         eq={"promotion_rule_action_id": 1, "promotion_rule_event_id": None})
    repo.paged.assert_called_once_with(query, page)


def test_repo_get_selects_embeds():
    repo = PromotionRuleAdminRepo.__new__(PromotionRuleAdminRepo)
    repo.one = MagicMock(return_value=_row())
    assert repo.get(RULE_ID)["id"] == RULE_ID
    repo.one.assert_called_once_with(RULE_ID, RULE_SELECT)


def test_lookup_still_works(monkeypatch):
    repo = PromotionRuleAdminRepo.__new__(PromotionRuleAdminRepo)
    query = MagicMock(name="query")
    repo.q = MagicMock(return_value=query)
    repo.filters = MagicMock(return_value=query)
    repo.paged = MagicMock(return_value=Paged(rows=[], total=0))

    repo.lookup("cash", PageParams(), None)

    repo.filters.assert_called_once_with(query, ilike={"name": "cash"}, eq={"id": None})
