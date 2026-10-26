"""Groups pages of the admin portal (MS-5085): list, detail, add, edit, delete, status, tag order.

Tags belong to a group; the group and its tags are saved in one request and one transaction
(SQL functions of ``migrations/admin/070_groups.sql``, audited, ``APP_CACHE_KEY`` rotated).

Icons go through ``MediaStorageService`` (bucket ``media``, folder = the group name), as the
portal uploads them today, but now in this order:
- create / edit: upload the new icons, then the transaction; when it fails every file just
  uploaded is removed (``remove_staged``), so nothing is left behind (MS-5070 for groups);
- edit: the replaced and removed icons, and the icons of the deleted tags, are removed only after
  the commit (``remove_after_commit``);
- delete: the usage check runs inside the transaction (409 ``linked_bundles`` with the number of
  bundles) and the icons are removed only after a successful delete. Fixes MS-5064, where the
  portal deleted the icons first and then found out the group was in use.

The region and country ``data`` of a tag is built here, with one rule for create and edit and no
URL encoding (fixes MS-5066: ``Middle%20East`` on create, ``Middle East`` on edit).
"""
from typing import Any, BinaryIO, Dict, Iterable, List, Optional
from uuid import uuid4

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.base import AuditContext
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.tag_repo import TagAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.groups import (AdminGroupCreate, AdminGroupTagInput, AdminGroupUpdate,
                                      normalize_alternative_country)
from app.services.admin.media_storage_service import MediaStorageService, StoredMedia

#: Every group write rotates APP_CACHE_KEY: add, edit, reorder, delete and the status switch.
#: The portal rotated it on add, edit and reorder only; the user decided (2026-10-21) to rotate on every
#: write so the storefront never shows a deleted or switched-off group from its cache.
ROTATE_CACHE = True
REGIONS_GROUP = "regions"
COUNTRIES_GROUP = "countries"


def _code(name: str) -> str:
    """``region_code`` / ``country_code`` from a name: spaces become underscores, nothing else changes."""
    return (name or "").replace(" ", "_")


def build_tag_data(group_name: str, tag_name: str, tag_id: str, icon_url: Optional[str],
                   alternative_country, extra: Optional[Dict[str, Any]] = None,
                   existing: Optional[Dict[str, Any]] = None, is_new: bool = True) -> Dict[str, Any]:
    """``tag.data`` as the portal's groupsAPI.jsx builds it, with the names kept raw.

    - Regions: ``guid``, ``icon``, ``zone_name``, ``region_code``, ``region_name`` are (re)built from the
      tag name on every save;
    - Countries: a country created from the admin gets the shape of a synced country
      (``id, icon, country, iso3_code, zone_name, country_code, operator_list``); an existing country
      keeps its synced data;
    - every tag: ``alternative_country`` = the search keywords as a comma-separated string or null.
    ``existing`` is the current data of an edited tag; ``extra`` is the ``data`` sent in the payload.
    """
    data: Dict[str, Any] = dict(existing or {})
    data.update(extra or {})
    group = (group_name or "").strip().lower()
    if group == REGIONS_GROUP:
        data.update({"guid": tag_id, "icon": icon_url, "zone_name": tag_name, "region_code": _code(tag_name),
                     "region_name": tag_name})
    elif group == COUNTRIES_GROUP and is_new:
        code = _code(tag_name)
        data.update({"id": tag_id, "icon": icon_url, "country": tag_name, "iso3_code": code,
                     "zone_name": "Unknown", "country_code": code, "operator_list": None})
    data["alternative_country"] = normalize_alternative_country(alternative_country)
    return data


class GroupAdminService:
    def __init__(self, repo: Optional[TagGroupAdminRepo] = None, tags: Optional[TagAdminRepo] = None,
                 media: Optional[MediaStorageService] = None):
        self._repo = repo
        self._tags = tags
        self._media = media

    # created lazily so importing the router never opens a database or storage client
    @property
    def repo(self) -> TagGroupAdminRepo:
        if self._repo is None:
            self._repo = TagGroupAdminRepo()
        return self._repo

    @property
    def tags_repo(self) -> TagAdminRepo:
        if self._tags is None:
            self._tags = TagAdminRepo()
        return self._tags

    @property
    def media(self) -> MediaStorageService:
        if self._media is None:
            self._media = MediaStorageService()
        return self._media

    # ---------- reads ----------

    def list(self, search: Optional[str], status: str, page: PageParams, type: Optional[int] = None,
             group_category: Optional[str] = None) -> AdminPage:
        """Each row carries ``tag_count`` and ``bundle_count`` (distinct bundles, one SQL call for the page:
        TEOS-173)."""
        result = self.repo.search(search, status, page, type=type, group_category=group_category)
        counts = self.repo.bundle_counts([row.get("id") for row in result.rows])
        data = [{**self._group(row), "bundle_count": counts.get(row.get("id"), 0)} for row in result.rows]
        return AdminPage(data=data, total_count=result.total,
                         facets=self.repo.facets(search, type=type, group_category=group_category))

    def get(self, group_id: int, tag_search: Optional[str] = None) -> Dict[str, Any]:
        """The group with its tags (filtered by ``tag_search``), plus ``top_countries_count`` for Countries
        and ``bundle_count`` (TEOS-173)."""
        row = self.repo.get_with_tags(group_id, tag_search)
        if row is None:
            raise AdminNotFound("not_found")
        top = None
        if (row.get("name") or "").strip().lower() == COUNTRIES_GROUP:
            top = self.repo.top_countries_count()
        return {**self._detail(row, row.get("tag") or [], top), "bundle_count": self._bundle_count(group_id)}

    def tags(self, group_id: int) -> List[Dict[str, Any]]:
        self.repo.get_or_404(group_id, select="id")
        return [self._tag(row) for row in self.tags_repo.by_group(group_id)]

    def read_image(self, file: BinaryIO) -> bytes:
        """Reads an uploaded file, at most one byte over the limit (enough for the 413 check)."""
        return file.read(self.media.max_bytes + 1)

    # ---------- writes ----------

    def create(self, payload: AdminGroupCreate, files: Dict[str, bytes], ctx: AuditContext) -> Dict[str, Any]:
        """Uploads the icons, then creates the group and its tags in one transaction (201).
        Any failure after an upload removes the uploaded files."""
        keys = {tag.client_key for tag in payload.tags}
        self._only_known_files(files, keys)
        self._check_names(payload.name, [tag.name for tag in payload.tags])
        group = payload.model_dump(include={"name", "type", "group_category"})
        uploaded: Dict[str, StoredMedia] = {}
        try:
            for key, data in files.items():
                uploaded[key] = self.media.upload(group["name"], data)
            rows = [self._new_tag_row(group["name"], tag, uploaded.get(tag.client_key)) for tag in payload.tags]
            result = self.repo.insert_group_with_tags(group, rows, ctx, rotate_cache=ROTATE_CACHE)
        except Exception:
            self.media.remove_staged([stored.path for stored in uploaded.values()])
            raise
        return self._from_result(result)

    def update(self, group_id: int, payload: AdminGroupUpdate, files: Dict[str, bytes],
               ctx: AuditContext) -> Dict[str, Any]:
        """Group fields, new tags, edited tags and deleted tags in one transaction.
        Replaced, removed and deleted icons are removed from storage after the commit."""
        current = self.repo.get_with_tags(group_id)
        if current is None:
            raise AdminNotFound("not_found")
        existing = {str(tag["id"]).lower(): tag for tag in current.get("tag") or []}

        updated_ids = [tag.id.lower() for tag in payload.updated_tags]
        deleted_ids = [tag_id.lower() for tag_id in payload.deleted_tag_ids]
        if len(set(updated_ids)) != len(updated_ids):
            raise AdminValidationError("updated_tags: duplicated id")
        foreign = [tag_id for tag_id in updated_ids + deleted_ids if tag_id not in existing]
        if foreign:
            raise AdminValidationError(f"tag_not_in_group: {', '.join(foreign)}")
        both = set(updated_ids) & set(deleted_ids)
        if both:
            raise AdminValidationError(f"a tag cannot be updated and deleted: {', '.join(sorted(both))}")

        new_keys = {tag.client_key for tag in payload.new_tags}
        files = {key.lower() if key.lower() in existing else key: data for key, data in files.items()}
        self._only_known_files(files, new_keys | set(updated_ids))

        # the tag names once the edit is applied: kept tags (renamed when updated) plus the new ones
        renamed = {tag.id.lower(): tag.name for tag in payload.updated_tags}
        final_names = [renamed.get(tag_id, tag.get("name") or "") for tag_id, tag in existing.items()
                       if tag_id not in deleted_ids] + [tag.name for tag in payload.new_tags]
        self._check_names(payload.name, final_names, group_id=group_id)

        group = payload.model_dump(include={"name", "type", "group_category"})
        uploaded: Dict[str, StoredMedia] = {}
        to_remove: List[Optional[str]] = []
        try:
            for key, data in files.items():
                uploaded[key] = self.media.upload(group["name"], data)
            new_rows = [self._new_tag_row(group["name"], tag, uploaded.get(tag.client_key))
                        for tag in payload.new_tags]
            updated_rows = []
            for tag in payload.updated_tags:
                tag_id = tag.id.lower()
                cur = existing[tag_id]
                stored = uploaded.get(tag_id)
                if stored is not None:
                    icon = stored.url
                    to_remove.append(self.media.path_from_url(cur.get("icon")))
                elif tag.remove_icon:
                    icon = None
                    to_remove.append(self.media.path_from_url(cur.get("icon")))
                else:
                    icon = cur.get("icon")
                data = build_tag_data(group["name"], tag.name, str(cur["id"]), icon, tag.alternative_country,
                                      tag.data, existing=cur.get("data"), is_new=False)
                updated_rows.append({"id": str(cur["id"]), "name": tag.name, "icon": icon, "data": data})
            for tag_id in deleted_ids:
                to_remove.append(self.media.path_from_url(existing[tag_id].get("icon")))
            result = self.repo.edit_tag_group(group_id, group, new_rows, updated_rows,
                                              [str(existing[tag_id]["id"]) for tag_id in deleted_ids], ctx,
                                              rotate_cache=ROTATE_CACHE)
        except Exception:
            self.media.remove_staged([stored.path for stored in uploaded.values()])
            raise
        fresh = {stored.path for stored in uploaded.values()}
        paths = [path for path in to_remove if path and path not in fresh]
        if paths:
            self.media.remove_after_commit(paths, reason=f"group {group_id} edited")
        return {**self._from_result(result), "bundle_count": self._bundle_count(group_id)}

    def delete(self, group_id: int, ctx: AuditContext) -> None:
        """Audited delete of the group and its tags; 409 ``linked_bundles`` (with ``bundle_count``) when a
        tag is assigned to a bundle, and then no icon is touched. Icons are removed after the commit."""
        result = self.repo.delete_group(group_id, ctx, rotate_cache=ROTATE_CACHE)
        paths = self._icon_paths((result or {}).get("tags") or [])
        if paths:
            self.media.remove_after_commit(paths, reason=f"group {group_id} deleted")

    def set_status(self, group_id: int, is_active: bool, ctx: AuditContext) -> Dict[str, Any]:
        """404 unknown group; 409 ``no_change`` when already in that state."""
        row = self.repo.update_audited(group_id, {"is_active": is_active}, ctx, rotate_cache=ROTATE_CACHE)
        return {"id": row.get("id"), "is_active": bool(row.get("is_active"))}

    def reorder(self, group_id: int, tag_ids: List[str], ctx: AuditContext) -> Dict[str, Any]:
        """``sorting_number`` = position in ``tag_ids``; 422 when a tag is missing or belongs elsewhere.
        The order already stored, or ``[]`` on a group without tags, is 200 with no audit row and no cache
        rotation (decided in ``admin_reorder_tags``: TEOS-197, TEOS-198)."""
        result = self.repo.reorder_tags(group_id, tag_ids, ctx, rotate_cache=ROTATE_CACHE)
        return {"tag_ids": [str(tag_id) for tag_id in (result or {}).get("tag_ids") or tag_ids]}

    # ---------- helpers ----------

    def _check_names(self, group_name: str, tag_names: List[str], group_id: Optional[int] = None) -> None:
        """TEOS-186, checked before any upload: group and tag names are unique ignoring case.
        409 ``duplicate_group_name`` when another group has the name in another case, 409
        ``duplicate_tag_name`` when two tags of the group would only differ by case (the exact
        duplicates are still refused by the database constraints)."""
        if self.repo.name_taken(group_name, exclude_id=group_id):
            raise AdminConflict("duplicate_group_name")
        seen = set()
        for name in tag_names:
            key = (name or "").strip().casefold()
            if key in seen:
                raise AdminConflict("duplicate_tag_name")
            seen.add(key)

    def _bundle_count(self, group_id: int) -> int:
        return self.repo.bundle_counts([group_id]).get(int(group_id), 0)

    @staticmethod
    def _only_known_files(files: Dict[str, bytes], keys: Iterable[str]) -> None:
        unknown = sorted(set(files) - set(keys))
        if unknown:
            raise AdminValidationError(f"icon for unknown tag(s): {', '.join(unknown)}")

    @staticmethod
    def _new_tag_row(group_name: str, tag: AdminGroupTagInput, stored: Optional[StoredMedia]) -> Dict[str, Any]:
        tag_id = str(uuid4())
        icon = stored.url if stored is not None else None
        return {"id": tag_id, "name": tag.name, "icon": icon,
                "data": build_tag_data(group_name, tag.name, tag_id, icon, tag.alternative_country, tag.data)}

    def _icon_paths(self, tags: Iterable[Dict[str, Any]]) -> List[str]:
        paths = [self.media.path_from_url(tag.get("icon")) for tag in tags]
        return [path for path in paths if path]

    def _from_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        result = result or {}
        return self._detail(result.get("group") or {}, result.get("tags") or [], None)

    @staticmethod
    def _group(row: Dict[str, Any]) -> Dict[str, Any]:
        embedded = row.get("tag")
        if isinstance(embedded, list) and embedded and isinstance(embedded[0], dict) and "count" in embedded[0]:
            tag_count = int(embedded[0].get("count") or 0)
        elif isinstance(embedded, list):
            tag_count = len(embedded)
        else:
            tag_count = int(row.get("tag_count") or 0)
        return {
            "id": row.get("id"),
            "name": row.get("name"),
            "type": row.get("type"),
            "group_category": row.get("group_category"),
            "is_active": bool(row.get("is_active", True)),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "tag_count": tag_count,
        }

    @staticmethod
    def _tag(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(row.get("id")),
            "tag_group_id": row.get("tag_group_id"),
            "name": row.get("name"),
            "icon": row.get("icon"),
            "sorting_number": row.get("sorting_number"),
            "data": row.get("data") if isinstance(row.get("data"), dict) else None,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def _detail(self, row: Dict[str, Any], tags: List[Dict[str, Any]], top: Optional[int]) -> Dict[str, Any]:
        shaped = sorted((self._tag(tag) for tag in tags),
                        key=lambda tag: (tag["sorting_number"] if tag["sorting_number"] is not None else 10000,
                                         (tag["name"] or "").lower()))
        group = self._group({**row, "tag": tags})
        return {**group, "tags": shaped, "top_countries_count": top}
