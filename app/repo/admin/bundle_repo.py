"""bundle for the admin API.

- ``lookup``: MS-5077 (bundle dropdowns on promotions, promo analysis);
- list, detail, rename, status, bulk status and tag set: MS-5083 (bundles page, assign to groups).

Reads go through PostgREST, except a search by text or by tags, which needs the ``bundle_tag``
join and therefore the ``admin_search_bundles`` SQL function (migrations/admin/060). Writes go
through the audited SQL functions of migrations/admin/061 (``admin_set_bundle_status``,
``admin_set_all_bundles_status``, ``admin_set_bundle_tags``), which rotate APP_CACHE_KEY and write
the audit row in the same transaction, as the portal did from the browser in three calls.
"""
from typing import Any, Dict, List, Optional, Sequence

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, AuditContext, Paged
from app.schemas.admin.common import PageParams

LOOKUP_SELECT = "id,display_title:data->>display_title,bundle_code:data->>bundle_code"
LIST_SELECT = "id,bundle_name,is_active,created_at,updated_at,data"
DETAIL_SELECT = (
    "id,bundle_name,is_active,created_at,updated_at,data,"
    "bundle_tag(id,tag(id,name,icon,tag_group(id,name,type,group_category,is_active)))"
)


def _has_text(value: Optional[str]) -> bool:
    return value is not None and bool(str(value).strip())


class BundleAdminRepo(AdminBaseRepository):
    #: admin_set_bundle_tags: 422 developerMessage {"code": "inactive_group_tag", "tag_ids": [...]} (TEOS-168)
    STRUCTURED_VALIDATION_CODES = frozenset({"inactive_group_tag"})

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BUNDLE)

    # ---------- lookup (MS-5077) ----------

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, display_title, bundle_code}`` rows; ``search`` matches ``data->>bundle_code``
        literally (as the portal's bundle dropdowns do). Ordered by display title, then id."""
        query = self.filters(self.q(LOOKUP_SELECT), eq={"id": record_id})
        if search is not None and search.strip():
            # JSON path: not a plain column, so it cannot go through filters()/or_ilike (column guard)
            query = query.ilike("data->>bundle_code", self.like(search.strip()))
        query = query.order("data->>display_title")
        return self.paged(query, page, order=(("id", False),))

    # ---------- list (MS-5083) ----------

    @staticmethod
    def needs_search_function(search: Optional[str], tag_ids: Optional[Sequence[str]]) -> bool:
        """A text search (bundle_name and the JSON fields) or a tag filter needs ``admin_search_bundles``."""
        return _has_text(search) or bool(tag_ids)

    def _status_filter(self, query, status: str):
        # ``is_active`` may be NULL on old rows: NULL counts as inactive, as the legacy SQL did
        # (``is_active IS NOT TRUE``), so the tab counts always add up.
        if status == "active":
            return query.is_("is_active", "true")
        if status == "inactive":
            return query.not_.is_("is_active", "true")
        return query

    def list_plain(self, status: str, page: PageParams) -> Paged:
        """One page without search or tags: PostgREST count + range, newest first."""
        return self.paged(self._status_filter(self.q(LIST_SELECT), status), page)

    def search(self, search: Optional[str], tag_ids: Optional[Sequence[str]], status: str,
               page: PageParams) -> Dict[str, Any]:
        """``{items, total_count, facets}`` from ``admin_search_bundles``; the facets count the rows
        matching the search and the tags whatever the status."""
        is_active = {"active": True, "inactive": False}.get(status)
        result = self.rpc("admin_search_bundles", {
            "p_search": search.strip() if _has_text(search) else None,
            "p_tag_ids": [str(t) for t in tag_ids] if tag_ids else None,
            "p_is_active": is_active,
            "p_page": page.page,
            "p_page_size": page.page_size,
        })
        result = result if isinstance(result, dict) else {}
        return {
            "items": result.get("items") or [],
            "total_count": int(result.get("total_count") or 0),
            "facets": result.get("facets") or {"all": 0, "active": 0, "inactive": 0},
        }

    def facets(self) -> Dict[str, int]:
        """``{all, active, inactive}`` over the whole table (no search, no tags): two head counts."""
        total = self.count()
        active = self.count(eq={"is_active": True})
        return {"all": total, "active": active, "inactive": max(total - active, 0)}

    def totals(self) -> Dict[str, int]:
        """Status breakdown of the whole table, for the "Activate all" / "Deactivate all" buttons."""
        facets = self.facets()
        return {"total": facets["all"], "active_count": facets["active"], "inactive_count": facets["inactive"]}

    # ---------- detail ----------

    def detail(self, bundle_id: str) -> Optional[Dict[str, Any]]:
        """The bundle with its ``bundle_tag`` rows, each with the tag and the tag's group; None when missing."""
        response = self._run(lambda: self.table.select(DETAIL_SELECT).eq("id", bundle_id).limit(1).execute())
        return response.data[0] if response.data else None

    # ---------- writes (SQL: migrations/admin/061_bundles_admin.sql) ----------

    def rename(self, bundle_id: str, bundle_name: str, ctx: AuditContext) -> Dict[str, Any]:
        """Audited update of ``bundle_name``; APP_CACHE_KEY rotates (the storefront shows the name)."""
        return self.update_audited(bundle_id, {"bundle_name": bundle_name}, ctx, rotate_cache=True)

    def set_status(self, bundle_id: str, is_active: bool, ctx: AuditContext) -> Dict[str, Any]:
        """``{id, is_active}``. AD404 -> 404; AD409 no_change when already in that state."""
        return self.rpc("admin_set_bundle_status", {
            "p_id": str(bundle_id), "p_is_active": is_active,
            "p_actor": ctx.actor_id, "p_ip": ctx.ip, "p_reason": ctx.reason,
        })

    def set_all_status(self, is_active: bool, ctx: AuditContext) -> Dict[str, Any]:
        """``{rows_affected, affected_bundle_ids}``. AD409 no_change when no bundle would change."""
        return self.rpc("admin_set_all_bundles_status", {
            "p_is_active": is_active, "p_actor": ctx.actor_id, "p_ip": ctx.ip, "p_reason": ctx.reason,
        })

    def set_tags(self, bundle_id: str, tag_ids: List[str], ctx: AuditContext) -> Dict[str, Any]:
        """``{id, tag_ids, added, removed}``. AD404 unknown bundle; AD422 unknown_tag; AD422
        inactive_group_tag (any tag of an inactive group, already-linked tags included, TEOS-168)."""
        return self.rpc("admin_set_bundle_tags", {
            "p_bundle_id": str(bundle_id), "p_tag_ids": [str(t) for t in tag_ids],
            "p_actor": ctx.actor_id, "p_ip": ctx.ip, "p_reason": ctx.reason,
        })
