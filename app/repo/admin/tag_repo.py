"""tag for the admin API.

- ``lookup``: MS-5077 (tag filter on the bundles page; replaces the ``get_tags_with_group_all`` RPC);
- ``by_group``: MS-5085 (``GET /groups/{id}/tags``). Tag writes go through the group SQL functions
  (``TagGroupAdminRepo``), never row by row.
"""
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams


class TagAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_TAG)

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, name, tag_group: {name}}`` rows matching ``search`` on the tag name, ordered by name, id."""
        query = self.filters(self.q("id,name,tag_group(name)"), ilike={"name": search}, eq={"id": record_id})
        return self.paged(query, page, order=(("name", False), ("id", False)))

    def by_group(self, group_id: int) -> List[Dict[str, Any]]:
        """Every tag of a group, ordered by sorting_number then name (the order the app shows)."""
        query = self.table.select("*").eq("tag_group_id", group_id).order("sorting_number").order("name")
        return self._run(lambda: query.execute()).data or []
