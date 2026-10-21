"""tag_group (bundle groups) for the admin API.

- ``lookup``: MS-5077 (group dropdowns);
- list, detail, create, edit, delete, status and reorder: MS-5085. The multi-row writes go through
  the ``admin_*`` SQL functions of ``migrations/admin/070_groups.sql``, which write the audit row
  and rotate ``APP_CACHE_KEY`` in the same transaction.
"""
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, AuditContext, Paged
from app.schemas.admin.common import PageParams

_STATUS_ACTIVE = {"active": True, "inactive": False}
TOP_COUNTRIES_KEY = "TOP_COUNTRIES_COUNT"
#: Tags embedded in a group, ordered the way the app and the reorder page show them.
TAG_ORDER = (("sorting_number", False), ("name", False))


class TagGroupAdminRepo(AdminBaseRepository):
    # tag_group_name_key: unique on tag_group.name (supabase_ddl.sql);
    # unique_tag_name_per_group: unique tag(name, tag_group_id) (migrations/admin/020_constraints.sql)
    UNIQUE_CONFLICTS = {"tag_group_name_key": "duplicate_group_name",
                        "unique_tag_name_per_group": "duplicate_tag_name"}

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_TAG_GROUP)

    # ---------- reads ----------

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, name, type, group_category}`` rows matching ``search`` on name, ordered by name, id."""
        query = self.filters(self.q("id,name,type,group_category"), ilike={"name": search}, eq={"id": record_id})
        return self.paged(query, page, order=(("name", False), ("id", False)))

    def search(self, search: Optional[str], status: str, page: PageParams) -> Paged:
        """One page of groups whose name contains ``search``, filtered by status, newest first.
        Each row carries ``tag: [{"count": n}]`` (PostgREST embedded count)."""
        query = self.filters(self.q("*, tag(count)"), ilike={"name": search},
                             eq={"is_active": _STATUS_ACTIVE.get(status)})
        return self.paged(query, page)

    def facets(self, search: Optional[str]) -> Dict[str, int]:
        """Status tab counts over the searched set (the status filter itself is not applied)."""
        total = self.count(ilike={"name": search})
        active = self.count(ilike={"name": search}, eq={"is_active": True})
        return {"all": total, "active": active, "inactive": max(total - active, 0)}

    def get_with_tags(self, group_id: int, tag_search: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The group with its tags embedded as ``tag: [...]`` ordered by sorting_number then name;
        ``tag_search`` keeps only the tags whose name contains it. None when the group does not exist."""
        query = self.table.select("*, tag(*)").eq("id", group_id)
        if tag_search is not None and str(tag_search).strip():
            query = query.ilike("tag.name", self.like(str(tag_search).strip()))
        for column, desc in TAG_ORDER:
            query = query.order(column, desc=desc, foreign_table="tag")
        response = self._run(lambda: query.limit(1).execute())
        return response.data[0] if response.data else None

    def top_countries_count(self) -> Optional[int]:
        """The ``TOP_COUNTRIES_COUNT`` setting as an integer, None when missing or not a number."""
        response = self._run(lambda: self.client.table(DatabaseTables.TABLE_APP_CONFIG).select("value")
                             .eq("key", TOP_COUNTRIES_KEY).limit(1).execute())
        rows = response.data or []
        if not rows:
            return None
        try:
            return int(str(rows[0].get("value")).strip())
        except (TypeError, ValueError):
            return None

    # ---------- writes (SQL: migrations/admin/070_groups.sql) ----------

    def insert_group_with_tags(self, group: Dict[str, Any], tags: List[Dict[str, Any]], ctx: AuditContext,
                               rotate_cache: bool = True) -> Dict[str, Any]:
        """Creates the group and its tags in one transaction; returns ``{group, tags}``."""
        return self.rpc("admin_insert_group_with_tags", {
            "p_name": group["name"], "p_type": group["type"], "p_group_category": group["group_category"],
            "p_tags": tags, "p_actor": ctx.actor_id, "p_ip": ctx.ip, "p_reason": ctx.reason,
            "p_rotate_cache": rotate_cache,
        })

    def edit_tag_group(self, group_id: int, group: Dict[str, Any], new_tags: List[Dict[str, Any]],
                       updated_tags: List[Dict[str, Any]], deleted_tag_ids: List[str], ctx: AuditContext,
                       rotate_cache: bool = True) -> Dict[str, Any]:
        """Updates the group, inserts, updates and deletes tags in one transaction; returns ``{group, tags}``."""
        return self.rpc("admin_edit_tag_group", {
            "p_id": group_id, "p_name": group["name"], "p_type": group["type"],
            "p_group_category": group["group_category"], "p_new_tags": new_tags, "p_updated_tags": updated_tags,
            "p_deleted_tag_ids": deleted_tag_ids, "p_actor": ctx.actor_id, "p_ip": ctx.ip,
            "p_reason": ctx.reason, "p_rotate_cache": rotate_cache,
        })

    def delete_group(self, group_id: int, ctx: AuditContext, rotate_cache: bool = True) -> Dict[str, Any]:
        """Deletes the group and its tags; returns ``{group, tags}`` as they were.
        AD409 ``linked_bundles`` (with ``bundle_count``) when a tag is assigned to a bundle."""
        return self.rpc("admin_delete_group", {
            "p_id": group_id, "p_actor": ctx.actor_id, "p_ip": ctx.ip, "p_reason": ctx.reason,
            "p_rotate_cache": rotate_cache,
        })

    def reorder_tags(self, group_id: int, tag_ids: List[str], ctx: AuditContext,
                     rotate_cache: bool = True) -> Dict[str, Any]:
        """``sorting_number`` = position in ``tag_ids`` (the full set of the group's tags); returns ``{tag_ids}``."""
        return self.rpc("admin_reorder_tags", {
            "p_group_id": group_id, "p_tag_ids": tag_ids, "p_actor": ctx.actor_id, "p_ip": ctx.ip,
            "p_reason": ctx.reason, "p_rotate_cache": rotate_cache,
        })
