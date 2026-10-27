"""tag_group (bundle groups) for the admin API.

- ``lookup``: MS-5077 (group dropdowns);
- list, detail, create, edit, delete, status and reorder: MS-5085. The multi-row writes go through
  the ``admin_*`` SQL functions of supabase_ddl.sql, section 070_groups.sql, which write the audit row
  and rotate ``APP_CACHE_KEY`` in the same transaction.
"""
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, AuditContext, Paged
from app.schemas.admin.common import PageParams

_STATUS_ACTIVE = {"active": True, "inactive": False}
TOP_COUNTRIES_KEY = "TOP_COUNTRIES_COUNT"
#: Default tag.sorting_number (supabase_ddl.sql), used for rows that have none.
DEFAULT_SORTING_NUMBER = 10000


def _tag_order(tag: Dict[str, Any]):
    """Tags in the order the app and the reorder page show them: sorting_number, then name."""
    number = tag.get("sorting_number")
    return (number if number is not None else DEFAULT_SORTING_NUMBER, (tag.get("name") or "").lower())


class TagGroupAdminRepo(AdminBaseRepository):
    # tag_group_name_key: unique on tag_group.name (supabase_ddl.sql);
    # unique_tag_name_per_group: unique tag(name, tag_group_id) (supabase_ddl.sql, section 020_constraints.sql)
    UNIQUE_CONFLICTS = {"tag_group_name_key": "duplicate_group_name",
                        "unique_tag_name_per_group": "duplicate_tag_name"}

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_TAG_GROUP)

    # ---------- reads ----------

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, name, type, group_category, is_active}`` rows matching ``search`` on name, ordered by name, id."""
        query = self.filters(self.q("id,name,type,group_category,is_active"), ilike={"name": search}, eq={"id": record_id})
        return self.paged(query, page, order=(("name", False), ("id", False)))

    def search(self, search: Optional[str], status: str, page: PageParams, type: Optional[int] = None,
               group_category: Optional[str] = None) -> Paged:
        """One page of groups whose name contains ``search``, filtered by status, ``type`` and
        ``group_category``, newest first. Each row carries ``tag: [{"count": n}]`` (PostgREST embedded count)."""
        query = self.filters(self.q("*, tag(count)"), ilike={"name": search},
                             eq={"is_active": _STATUS_ACTIVE.get(status), "type": type,
                                 "group_category": group_category})
        return self.paged(query, page)

    def facets(self, search: Optional[str], type: Optional[int] = None,
               group_category: Optional[str] = None) -> Dict[str, int]:
        """Status tab counts over the searched and filtered set (the status filter itself is not applied)."""
        eq = {"type": type, "group_category": group_category}
        total = self.count(ilike={"name": search}, eq=eq)
        active = self.count(ilike={"name": search}, eq={**eq, "is_active": True})
        return {"all": total, "active": active, "inactive": max(total - active, 0)}

    def get_with_tags(self, group_id: int, tag_search: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The group with its tags embedded as ``tag: [...]`` ordered by sorting_number then name;
        ``tag_search`` keeps only the tags whose name contains it. None when the group does not exist.

        The tags are sorted here, not by PostgREST: ``order(..., foreign_table="tag")`` is sent as
        ``order=tag(sorting_number)``, a related order that PostgREST refuses on a to-many embed
        (PGRST118), which made every GET and PUT /groups/{id} fail with 502 (TEOS-180, TEOS-181)."""
        query = self.table.select("*, tag(*)").eq("id", group_id)
        if tag_search is not None and str(tag_search).strip():
            query = query.ilike("tag.name", self.like(str(tag_search).strip()))
        response = self._run(lambda: query.limit(1).execute())
        if not response.data:
            return None
        row = response.data[0]
        return {**row, "tag": sorted(row.get("tag") or [], key=_tag_order)}

    def bundle_counts(self, group_ids: List[int]) -> Dict[int, int]:
        """``{group_id: distinct bundles tagged with any tag of the group}`` (TEOS-173), counted in SQL by
        ``admin_group_bundle_counts`` in one call for the whole page. Groups without bundles map to 0;
        no call when ``group_ids`` is empty."""
        ids = [int(group_id) for group_id in dict.fromkeys(group_ids) if group_id is not None]
        if not ids:
            return {}
        counts = dict.fromkeys(ids, 0)
        for row in self.rpc("admin_group_bundle_counts", {"p_group_ids": ids}) or []:
            try:
                counts[int(row.get("group_id"))] = int(row.get("bundle_count") or 0)
            except (TypeError, ValueError):
                continue
        return counts

    def name_taken(self, name: str, exclude_id: Optional[int] = None) -> bool:
        """True when another group is called ``name`` ignoring case (TEOS-186). The exact-case duplicate
        is also refused by the unique constraint; this read covers the other cases."""
        exact = self.like(str(name or "").strip())[1:-1]  # escaped, without the surrounding %
        query = self.table.select("id").ilike("name", exact)
        if exclude_id is not None:
            query = query.neq("id", exclude_id)
        response = self._run(lambda: query.limit(1).execute())
        return bool(response.data)

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

    # ---------- writes (SQL: supabase_ddl.sql, section 070_groups.sql) ----------

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
        """Updates the group, inserts, updates and deletes tags in one transaction; returns ``{group, tags}``.
        AD409 ``linked_bundles`` when a deleted tag is assigned to a bundle, with ``bundle_count`` (distinct
        bundles), ``tag_ids`` and ``tags: [{id, name, bundle_count}]`` of the linked deleted tags (TEOS-196)."""
        return self.rpc("admin_edit_tag_group", {
            "p_id": group_id, "p_name": group["name"], "p_type": group["type"],
            "p_group_category": group["group_category"], "p_new_tags": new_tags, "p_updated_tags": updated_tags,
            "p_deleted_tag_ids": deleted_tag_ids, "p_actor": ctx.actor_id, "p_ip": ctx.ip,
            "p_reason": ctx.reason, "p_rotate_cache": rotate_cache,
        })

    def delete_group(self, group_id: int, ctx: AuditContext, rotate_cache: bool = True) -> Dict[str, Any]:
        """Deletes the group and its tags; returns ``{group, tags}`` as they were.
        AD409 ``linked_bundles`` (with ``bundle_count``, distinct bundles: TEOS-176) when a tag is assigned
        to a bundle."""
        return self.rpc("admin_delete_group", {
            "p_id": group_id, "p_actor": ctx.actor_id, "p_ip": ctx.ip, "p_reason": ctx.reason,
            "p_rotate_cache": rotate_cache,
        })

    def reorder_tags(self, group_id: int, tag_ids: List[str], ctx: AuditContext,
                     rotate_cache: bool = True) -> Dict[str, Any]:
        """``sorting_number`` = position in ``tag_ids`` (the full set of the group's tags); returns ``{tag_ids}``.
        The order already stored (and ``[]`` for a group without tags) writes nothing: no audit row, no
        cache rotation (TEOS-197, TEOS-198)."""
        return self.rpc("admin_reorder_tags", {
            "p_group_id": group_id, "p_tag_ids": tag_ids, "p_actor": ctx.actor_id, "p_ip": ctx.ip,
            "p_reason": ctx.reason, "p_rotate_cache": rotate_cache,
        })
