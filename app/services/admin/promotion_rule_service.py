"""Promotion rules page of the admin portal (MS-5087).

- list: literal search on ``id_text``, action and event filters, action/event joined by PostgREST, and
  ``promotion_count`` (promotions using the rule) counted in the same query (TEOS-210);
- actions and events: the two small reference tables;
- create and edit: uniqueness by the database (unique ``(action, event, max_usage)``, 409
  ``duplicate_rule``) instead of a browser read-then-write (MS-5070); audited in the same transaction;
- delete: refused by the foreign key from ``promotion.rule_id`` (409 ``rule_in_use``).
"""
from typing import Any, Dict, List, Optional

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.base import AuditContext
from app.repo.admin.promotion_rule_repo import (PromotionRuleActionAdminRepo, PromotionRuleAdminRepo,
                                                PromotionRuleEventAdminRepo)
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.promotion_rules import AdminPromotionRuleBody


def _promotion_count(row: Dict[str, Any]) -> int:
    """The PostgREST embedded count ``promotion: [{"count": n}]``; 0 when absent."""
    embedded = row.get("promotion")
    if isinstance(embedded, list) and embedded and isinstance(embedded[0], dict):
        return int(embedded[0].get("count") or 0)
    return 0


def shape_rule(row: Dict[str, Any]) -> Dict[str, Any]:
    """The list projection of a ``promotion_rule`` row with its PostgREST embeds."""
    action = row.get("promotion_rule_action") or (
        {"id": row.get("promotion_rule_action_id")} if row.get("promotion_rule_action_id") is not None else None)
    event = row.get("promotion_rule_event") or (
        {"id": row.get("promotion_rule_event_id")} if row.get("promotion_rule_event_id") is not None else None)
    return {
        "id": str(row.get("id")),
        "id_text": row.get("id_text") or str(row.get("id")),
        "name": row.get("name"),
        "max_usage": row.get("max_usage"),
        "beneficiary": row.get("beneficiary"),
        "created_at": row.get("created_at"),
        "action": {"id": action["id"], "name": action.get("name")} if action else None,
        "event": {"id": event["id"], "name": event.get("name")} if event else None,
        "promotion_count": _promotion_count(row),
    }


class PromotionRuleAdminService:
    def __init__(self, repo: Optional[PromotionRuleAdminRepo] = None,
                 actions: Optional[PromotionRuleActionAdminRepo] = None,
                 events: Optional[PromotionRuleEventAdminRepo] = None):
        self._repo = repo
        self._actions = actions
        self._events = events

    # created lazily so importing the router never opens a database client
    @property
    def repo(self) -> PromotionRuleAdminRepo:
        if self._repo is None:
            self._repo = PromotionRuleAdminRepo()
        return self._repo

    @property
    def actions_repo(self) -> PromotionRuleActionAdminRepo:
        if self._actions is None:
            self._actions = PromotionRuleActionAdminRepo()
        return self._actions

    @property
    def events_repo(self) -> PromotionRuleEventAdminRepo:
        if self._events is None:
            self._events = PromotionRuleEventAdminRepo()
        return self._events

    # ---------- reads ----------

    def list(self, search: Optional[str], action_id: Optional[int], event_id: Optional[int],
             page: PageParams) -> AdminPage:
        result = self.repo.search(search, action_id, event_id, page)
        return AdminPage(data=[shape_rule(row) for row in result.rows], total_count=result.total)

    def actions(self) -> List[Dict[str, Any]]:
        return [{"id": row["id"], "name": row.get("name")} for row in self.actions_repo.all()]

    def events(self) -> List[Dict[str, Any]]:
        return [{"id": row["id"], "name": row.get("name")} for row in self.events_repo.all()]

    def get(self, rule_id: str) -> Dict[str, Any]:
        row = self.repo.get(rule_id)
        if row is None:
            raise AdminNotFound("not_found")
        return shape_rule(row)

    # ---------- writes ----------

    def create(self, body: AdminPromotionRuleBody, ctx: AuditContext) -> Dict[str, Any]:
        """201. 409 duplicate_rule for an existing (action, event, max_usage); 422 unknown action or event."""
        self._check_action_and_event(body)
        row = self._write(lambda: self.repo.insert_audited(body.model_dump(), ctx))
        return self.get(row["id"])

    def update(self, rule_id: str, body: AdminPromotionRuleBody, ctx: AuditContext) -> Dict[str, Any]:
        """200 (also when nothing changed). 404; 409 duplicate_rule; 422 unknown action or event."""
        self._check_action_and_event(body)
        try:
            self._write(lambda: self.repo.update_audited(rule_id, body.model_dump(), ctx))
        except AdminConflict as exc:
            if exc.conflict_code != "no_change":
                raise
        return self.get(rule_id)

    def delete(self, rule_id: str, ctx: AuditContext) -> None:
        """204. 404 when missing; 409 rule_in_use when a promotion references the rule (FK 23503)."""
        try:
            self.repo.delete_audited(rule_id, ctx)
        except AdminConflict as exc:
            if exc.conflict_code == "referenced":
                raise AdminConflict("rule_in_use") from exc
            raise

    # ---------- helpers ----------

    def _check_action_and_event(self, body: AdminPromotionRuleBody) -> None:
        if body.promotion_rule_action_id not in {row["id"] for row in self.actions_repo.all()}:
            raise AdminValidationError("promotion_rule_action_id: unknown action")
        if body.promotion_rule_event_id not in {row["id"] for row in self.events_repo.all()}:
            raise AdminValidationError("promotion_rule_event_id: unknown event")

    @staticmethod
    def _write(call):
        """A foreign key failure on a write means an unknown action or event id (422, not 409)."""
        try:
            return call()
        except AdminConflict as exc:
            if exc.conflict_code == "referenced":
                raise AdminValidationError("unknown action or event id") from exc
            raise
