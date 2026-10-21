"""Bundles page and "assign bundle to groups" page of the admin portal (MS-5083).

- list: search by name or code, tag filter, status filter, status tab counts (``facets``, over the
  filtered set) and the whole-table breakdown (``totals``, for the bulk buttons) in one response;
- detail: the bundle with its tags and their groups (replaces two browser queries);
- rename, status, bulk status and tag set: audited and APP_CACHE_KEY rotated in the same
  transaction by the SQL functions, instead of three browser calls (MS-5071); the tag set may be
  empty (MS-5065).
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.repo.admin.base import AuditContext
from app.repo.admin.bundle_repo import BundleAdminRepo
from app.exceptions.admin import AdminNotFound
from app.schemas.admin.bundles import (
    AdminBundleBulkStatus,
    AdminBundleRename,
    AdminBundleStatusPatch,
    AdminBundleTagsSet,
)
from app.schemas.admin.common import AdminPage, PageParams


class BundleAdminService:
    def __init__(self, repo: Optional[BundleAdminRepo] = None):
        self._repo = repo

    # created lazily so importing the router never opens a database client
    @property
    def repo(self) -> BundleAdminRepo:
        if self._repo is None:
            self._repo = BundleAdminRepo()
        return self._repo

    # ---------- reads ----------

    def list(self, search: Optional[str], tag_ids: Optional[Sequence[str]], status: str,
             page: PageParams) -> AdminPage:
        """Plain count + range without search or tags, else ``admin_search_bundles``. ``totals``
        always covers the whole table."""
        if self.repo.needs_search_function(search, tag_ids):
            result = self.repo.search(search, tag_ids, status, page)
            rows, total, facets = result["items"], result["total_count"], result["facets"]
            totals = self.repo.totals()
        else:
            paged = self.repo.list_plain(status, page)
            rows, total = paged.rows, paged.total
            facets = self.repo.facets()
            # no search and no tags: the filtered set is the whole table
            totals = {"total": facets["all"], "active_count": facets["active"],
                      "inactive_count": facets["inactive"]}
        return AdminPage(data=[self._shape(row) for row in rows], total_count=total, facets=facets, totals=totals)

    def get(self, bundle_id: str) -> Dict[str, Any]:
        """404 when missing. ``tags`` carry the tag and its group, so the assign page can list the groups."""
        row = self.repo.detail(bundle_id)
        if row is None:
            raise AdminNotFound("not_found")
        shaped = self._shape(row)
        shaped["tags"] = self._shape_tags(row.get("bundle_tag") or [])
        return shaped

    # ---------- writes ----------

    def rename(self, bundle_id: str, body: AdminBundleRename, ctx: AuditContext) -> Dict[str, Any]:
        """404 when missing; 409 no_change when the name is already that (from the SQL function)."""
        row = self.repo.rename(bundle_id, body.bundle_name, ctx)
        return {"id": str(row.get("id")), "bundle_name": row.get("bundle_name")}

    def set_status(self, bundle_id: str, body: AdminBundleStatusPatch, ctx: AuditContext) -> Dict[str, Any]:
        """404 when missing; 409 no_change when the bundle already has that state."""
        row = self.repo.set_status(bundle_id, body.is_active, ctx)
        return {"id": str(row.get("id")), "is_active": bool(row.get("is_active"))}

    def set_all_status(self, body: AdminBundleBulkStatus, ctx: AuditContext) -> Dict[str, Any]:
        """Every bundle to ``is_active``; 409 no_change when none would change (nothing audited)."""
        row = self.repo.set_all_status(body.is_active, ctx)
        return {"rows_affected": int(row.get("rows_affected") or 0),
                "affected_bundle_ids": [str(i) for i in row.get("affected_bundle_ids") or []]}

    def set_tags(self, bundle_id: str, body: AdminBundleTagsSet, ctx: AuditContext) -> Dict[str, Any]:
        """Replaces the tag set exactly; ``[]`` clears it (MS-5065). 404 unknown bundle; 422 unknown tag."""
        tag_ids = list(dict.fromkeys(str(t) for t in body.tag_ids))  # unique, order kept
        row = self.repo.set_tags(bundle_id, tag_ids, ctx)
        return {"id": str(row.get("id")), "tag_ids": [str(i) for i in row.get("tag_ids") or []]}

    # ---------- helpers ----------

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(row.get("id")),
            "bundle_name": row.get("bundle_name"),
            "is_active": bool(row.get("is_active")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "data": row.get("data") if isinstance(row.get("data"), dict) else None,
        }

    @staticmethod
    def _shape_tags(links: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        tags = []
        for link in links:
            tag = link.get("tag") or {}
            if not tag.get("id"):
                continue  # dangling link (tag deleted meanwhile)
            group = tag.get("tag_group") or None
            tags.append({
                "bundle_tag_id": link.get("id"),
                "tag": {
                    "id": str(tag.get("id")),
                    "name": tag.get("name"),
                    "icon": tag.get("icon"),
                    "group": {
                        "id": group.get("id"), "name": group.get("name"), "type": group.get("type"),
                        "group_category": group.get("group_category"),
                    } if group and group.get("id") is not None else None,
                },
            })
        return tags
