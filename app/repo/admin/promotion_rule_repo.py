"""promotion_rule for the admin API.

- ``lookup``: MS-5077 (rule dropdowns on promotions);
- list, create, edit and delete are added by MS-5087.

``promotion_rule.name`` ("action/event/max usage") is filled by a trigger; the column is added on
older databases by ``migrations/admin/000_schema_drift.sql``.
"""
from typing import Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams


class PromotionRuleAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_PROMOTION_RULE)

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, name}`` rows matching ``search`` on name, ordered by name, id."""
        query = self.filters(self.q("id,name"), ilike={"name": search}, eq={"id": record_id})
        return self.paged(query, page, order=(("name", False), ("id", False)))
