"""tag_group (bundle groups) for the admin API.

- ``lookup``: MS-5077 (group dropdowns);
- list, detail, create, edit, delete, status and reorder are added by MS-5085.
"""
from typing import Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams


class TagGroupAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_TAG_GROUP)

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, name, type, group_category}`` rows matching ``search`` on name, ordered by name, id."""
        query = self.filters(self.q("id,name,type,group_category"), ilike={"name": search}, eq={"id": record_id})
        return self.paged(query, page, order=(("name", False), ("id", False)))
