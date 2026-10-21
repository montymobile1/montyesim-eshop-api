"""Images uploaded through the admin API (banner images, tag icons).

- ``validate``: real PNG/JPEG/WebP content (checked from the bytes, not the file name or the
  browser's content type), at most ``ADMIN_MAX_UPLOAD_BYTES`` (413) and 4000x4000 pixels (422).
- ``upload``: stores the file under a name the server generates and returns its public URL.
- ``remove_staged``: deletes files uploaded for a save that then failed (rollback).
- ``remove_after_commit``: deletes replaced or deleted files after the save succeeded.

Deletion never fails the request: a file that cannot be deleted is logged and added to
``storage_cleanup_queue`` for a later retry, so nothing is silently left behind.
"""
import io
import os
import re
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional
from urllib.parse import unquote, urlparse

from loguru import logger
from PIL import Image

from app.config.config import supabase_client
from app.exceptions.admin import AdminPayloadTooLarge, AdminUpstreamError, AdminValidationError
from app.repo.admin.storage_cleanup_queue_repo import StorageCleanupQueueRepo

DEFAULT_BUCKET = "media"
DEFAULT_MAX_UPLOAD_BYTES = 1024 * 1024  # 1 MB, as MS-5060 and MS-5086 ask (user decision 2026-10-21)
MAX_DIMENSION = 4000

_TYPES = {  # content type -> file extension
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, default))
        return value if value > 0 else default
    except ValueError:
        return default


def detect_image_type(data: bytes) -> Optional[str]:
    """Content type from the file's first bytes, or None if it is not PNG, JPEG or WebP."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _safe_folder(folder: str) -> str:
    """Folder segments reduced to lowercase letters, digits, ``-`` and ``_`` (no ``..``, no ``/`` tricks)."""
    segments = []
    for segment in str(folder or "").split("/"):
        cleaned = re.sub(r"[^a-z0-9_-]+", "-", segment.strip().lower()).strip("-")
        if cleaned:
            segments.append(cleaned)
    return "/".join(segments) or "uploads"


@dataclass(frozen=True)
class StoredMedia:
    path: str
    url: str
    content_type: str
    size: int


class MediaStorageService:
    def __init__(self, bucket: Optional[str] = None, client_factory: Callable = supabase_client,
                 cleanup_queue: Optional[StorageCleanupQueueRepo] = None):
        self.bucket = bucket or os.getenv("ADMIN_MEDIA_BUCKET") or DEFAULT_BUCKET
        self.max_bytes = _env_int("ADMIN_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        self._client_factory = client_factory
        self._cleanup_queue = cleanup_queue

    def _storage(self):
        return self._client_factory().storage.from_(self.bucket)

    # ---------- validation ----------

    def validate(self, data: bytes) -> str:
        """Returns the content type of a valid image; raises 413 or 422 otherwise."""
        if not data:
            raise AdminValidationError("empty_file")
        if len(data) > self.max_bytes:
            raise AdminPayloadTooLarge(f"file larger than {self.max_bytes} bytes")
        content_type = detect_image_type(data)
        if content_type is None:
            raise AdminValidationError("unsupported_image_type")
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                image.verify()
        except Exception:
            raise AdminValidationError("invalid_image")
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise AdminValidationError(f"image larger than {MAX_DIMENSION}x{MAX_DIMENSION} pixels")
        return content_type

    # ---------- upload ----------

    def upload(self, folder: str, data: bytes) -> StoredMedia:
        """Validates and stores ``data`` as ``<folder>/<uuid>.<ext>``; never overwrites a file."""
        content_type = self.validate(data)
        path = f"{_safe_folder(folder)}/{uuid.uuid4().hex}.{_TYPES[content_type]}"
        try:
            self._storage().upload(path, data, {"content-type": content_type, "cache-control": "3600",
                                                "upsert": "false"})
        except Exception as exc:
            logger.error(f"admin upload to {self.bucket}/{path} failed: {type(exc).__name__}: {exc}")
            raise AdminUpstreamError("STORAGE_ERROR")
        return StoredMedia(path=path, url=self.public_url(path), content_type=content_type, size=len(data))

    def public_url(self, path: str) -> str:
        return self._storage().get_public_url(path).rstrip("?")

    def path_from_url(self, url: Optional[str]) -> Optional[str]:
        """Object path from a public URL of this bucket (rows store full URLs), else None."""
        if not url:
            return None
        marker = f"/storage/v1/object/public/{self.bucket}/"
        location = urlparse(url).path
        if marker not in location:
            return None
        path = unquote(location.split(marker, 1)[1])
        return path or None

    # ---------- deletion ----------

    def remove_staged(self, paths: Iterable[Optional[str]]) -> None:
        """Rollback: deletes files uploaded for a save that failed."""
        self._remove(paths, reason="rollback of a failed save")

    def remove_after_commit(self, paths: Iterable[Optional[str]], reason: str) -> None:
        """Deletes files no longer referenced after a successful save (replaced or deleted)."""
        self._remove(paths, reason=reason)

    def _remove(self, paths: Iterable[Optional[str]], reason: str) -> None:
        targets: List[str] = sorted({p for p in paths if p})
        if not targets:
            return
        try:
            self._storage().remove(targets)
            return
        except Exception as exc:
            logger.error(f"admin storage delete in {self.bucket} failed ({reason}): {type(exc).__name__}: {exc}")
        queue = self._cleanup_queue or StorageCleanupQueueRepo()
        for path in targets:
            try:
                queue.enqueue(self.bucket, path, reason)
            except Exception as exc:
                logger.error(f"could not queue {self.bucket}/{path} for cleanup: {type(exc).__name__}")
