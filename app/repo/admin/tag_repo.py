"""tag for the admin API.

- ``lookup``: MS-5077 (tag filter on the bundles page; replaces the ``get_tags_with_group_all`` RPC);
- tag writes are added by the groups story (MS-5085).
"""
from typing import Optional

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
