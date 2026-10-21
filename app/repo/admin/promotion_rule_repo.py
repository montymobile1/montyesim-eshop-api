"""promotion_rule for the admin API.

- ``lookup``: MS-5077 (rule dropdowns on promotions);
- ``search``, ``get``, ``actions``, ``events`` and the audited writes: MS-5087 (rules page).

``promotion_rule.name`` ("action-event-max usage") is filled by a trigger; the column is added on
older databases by ``migrations/admin/000_schema_drift.sql``. ``id_text`` (the uuid as text, searched
with ilike) comes from ``030_promotion_rule_id_text.sql``.
"""
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

#: The list select: the rule plus its action and event (PostgREST embeds).
RULE_SELECT = "*,promotion_rule_action(id,name),promotion_rule_event(id,name)"


class PromotionRuleAdminRepo(AdminBaseRepository):
    # promotion_rule_action_event_max_usage_key: migrations/admin/020_constraints.sql
    UNIQUE_CONFLICTS = {"promotion_rule_action_event_max_usage_key": "duplicate_rule"}

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION_RULE)

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, name}`` rows matching ``search`` on name, ordered by name, id."""
        query = self.filters(self.q("id,name"), ilike={"name": search}, eq={"id": record_id})
        return self.paged(query, page, order=(("name", False), ("id", False)))

    # ---------- MS-5087 ----------

    def search(self, search: Optional[str], action_id: Optional[int], event_id: Optional[int],
               page: PageParams) -> Paged:
        """One page of rules (with action and event), ``search`` matched literally on ``id_text``,
        newest first."""
        query = self.filters(self.q(RULE_SELECT), ilike={"id_text": search},
                             eq={"promotion_rule_action_id": action_id, "promotion_rule_event_id": event_id})
        return self.paged(query, page)

    def get(self, rule_id: Any) -> Optional[Dict[str, Any]]:
        """One rule with its action and event, or None."""
        return self.one(rule_id, RULE_SELECT)


class PromotionRuleActionAdminRepo(AdminBaseRepository):
    """``promotion_rule_action``: the few action types (DISCOUNT_AMOUNT, CASHBACK_PERCENTAGE...)."""

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION_RULE_ACTION)

    def all(self) -> List[Dict[str, Any]]:
        return self._run(lambda: self.table.select("id,name").order("id").execute()).data or []


class PromotionRuleEventAdminRepo(AdminBaseRepository):
    """``promotion_rule_event``: the few event types (CREATE_ORDER, CREATE_ACCOUNT)."""

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION_RULE_EVENT)

    def all(self) -> List[Dict[str, Any]]:
        return self._run(lambda: self.table.select("id,name").order("id").execute()).data or []
