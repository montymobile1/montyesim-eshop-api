"""Banners pages of the admin portal (MS-5058 list and delete, MS-5060 add and edit).

The image goes through ``MediaStorageService``: the server checks the file, stores it under a
generated name in the ``media`` bucket, folder ``banners-web`` (where the portal uploads today),
and removes files itself:
- a failed save removes the file it just uploaded (``remove_staged``);
- a successful edit with a new image removes the replaced file, and a delete removes the banner's
  file (``remove_after_commit``; a failed removal is queued, never silently ignored). Fixes MS-5063.
The new image URL comes from the upload result, never from the browser (fixes MS-5062).

``banner.image`` keeps storing the full public URL: the app route GET /app/banners returns it as is.

APP_CACHE_KEY: the portal does not rotate it on banner writes today, so banner writes do not
rotate it either (``ROTATE_CACHE``; one line to change if the team decides otherwise).
"""
from typing import Any, BinaryIO, Dict, Optional

from app.exceptions.admin import AdminConflict
from app.repo.admin.banner_repo import BannerAdminRepo
from app.repo.admin.base import AuditContext
from app.schemas.admin.common import AdminPage, PageParams
from app.services.admin.media_storage_service import MediaStorageService

IMAGE_FOLDER = "banners-web"
ROTATE_CACHE = False


class BannerAdminService:
    def __init__(self, repo: Optional[BannerAdminRepo] = None, media: Optional[MediaStorageService] = None):
        self._repo = repo
        self._media = media

    # created lazily so importing the router never opens a database or storage client
    @property
    def repo(self) -> BannerAdminRepo:
        if self._repo is None:
            self._repo = BannerAdminRepo()
        return self._repo

    @property
    def media(self) -> MediaStorageService:
        if self._media is None:
            self._media = MediaStorageService()
        return self._media

    # ---------- reads ----------

    def list(self, search: Optional[str], status: str, page: PageParams) -> AdminPage:
        result = self.repo.search(search, status, page)
        return AdminPage(data=[self._shape(row) for row in result.rows], total_count=result.total,
                         facets=self.repo.facets(search))

    def get(self, banner_id: int) -> Dict[str, Any]:
        return self._shape(self.repo.get_or_404(banner_id))

    def read_image(self, file: BinaryIO) -> bytes:
        """Reads an uploaded file, at most one byte over the limit (enough for the 413 check)."""
        return file.read(self.media.max_bytes + 1)

    # ---------- writes ----------

    def create(self, fields: Dict[str, Any], image: bytes, ctx: AuditContext) -> Dict[str, Any]:
        """Upload, then the audited insert; the uploaded file is removed when the insert fails (409, 502...)."""
        stored = self.media.upload(IMAGE_FOLDER, image)
        try:
            row = self.repo.insert_audited({**fields, "image": stored.url}, ctx, rotate_cache=ROTATE_CACHE)
        except Exception:
            self.media.remove_staged([stored.path])
            raise
        return self._shape(row)

    def update(self, banner_id: int, fields: Dict[str, Any], image: Optional[bytes],
               ctx: AuditContext) -> Dict[str, Any]:
        """Edit; without ``image`` the current image is kept. Saving unchanged values returns the banner."""
        current = self.repo.get_or_404(banner_id)
        stored = self.media.upload(IMAGE_FOLDER, image) if image is not None else None
        patch = dict(fields)
        guard = None
        if stored is not None:
            patch["image"] = stored.url
            # the file removed after the commit must be the one this edit replaced
            # (another admin replacing the image meanwhile -> 409 guard_failed)
            guard = {"image": current.get("image")}
        try:
            row = self.repo.update_audited(banner_id, patch, ctx, rotate_cache=ROTATE_CACHE, guard=guard)
        except AdminConflict as exc:
            if stored is None and exc.conflict_code == "no_change":
                return self._shape(current)
            if stored is not None:
                self.media.remove_staged([stored.path])
            raise
        except Exception:
            if stored is not None:
                self.media.remove_staged([stored.path])
            raise
        if stored is not None:
            old_path = self.media.path_from_url(current.get("image"))
            if old_path and old_path != stored.path:
                self.media.remove_after_commit([old_path], reason=f"banner {banner_id} image replaced")
        return self._shape(row)

    def delete(self, banner_id: int, ctx: AuditContext) -> None:
        """Audited delete, then the image file is removed (queued for retry when storage fails)."""
        row = self.repo.delete_audited(banner_id, ctx, rotate_cache=ROTATE_CACHE)
        path = self.media.path_from_url((row or {}).get("image"))
        if path:
            self.media.remove_after_commit([path], reason=f"banner {banner_id} deleted")

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id"),
            "title": row.get("title"),
            "description": row.get("description"),
            "image_url": row.get("image"),
            "action": row.get("action"),
            "platform": row.get("platform"),
            "is_active": bool(row.get("is_active", True)),
            "created_at": row.get("created_at"),
        }
