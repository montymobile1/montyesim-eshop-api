import io
from unittest.mock import MagicMock

import pytest
from PIL import Image
from starlette.requests import Request

from app.exceptions.admin import AdminPayloadTooLarge, AdminUpstreamError, AdminValidationError
from app.repo.admin.base import AuditContext
from app.services.admin.audit_service import AuditService, audit_context
from app.services.admin.media_storage_service import MediaStorageService, detect_image_type
from tests.admin.conftest import make_admin

BUCKET_URL = "https://dummy.supabase.co/storage/v1/object/public/media/"


def _image(fmt="PNG", size=(10, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


# ---------- audit ----------

def test_audit_context_takes_actor_from_token_and_ip_from_headers():
    request = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"",
                       "headers": [(b"x-forwarded-for", b"9.9.9.9")], "client": ("10.0.0.1", 1)})

    ctx = audit_context(make_admin(), request, reason="spam")

    assert ctx == AuditContext(actor_id=make_admin().id, ip="9.9.9.9", reason="spam")


def test_audit_log_writes_through_admin_write_audit():
    repo = MagicMock()
    ctx = AuditContext(actor_id="admin-1", ip="1.2.3.4", reason="abuse")

    AuditService(repo).log(ctx, "auth.users", "BAN", record_id="u-1", old={"banned": False},
                           new={"banned": True}, rows_affected=1, hours=24)

    repo.write.assert_called_once_with(ctx, table="auth.users", operation="BAN", record_id="u-1",
                                       old_data={"banned": False}, new_data={"banned": True},
                                       details={"hours": 24, "rows_affected": 1})


# ---------- media: validation ----------

@pytest.fixture
def storage():
    return MagicMock(name="bucket")


@pytest.fixture
def queue():
    return MagicMock(name="cleanup-queue")


@pytest.fixture
def media(storage, queue, monkeypatch):
    monkeypatch.delenv("ADMIN_MAX_UPLOAD_BYTES", raising=False)
    client = MagicMock()
    client.storage.from_.return_value = storage
    storage.get_public_url.side_effect = lambda path: f"{BUCKET_URL}{path}?"
    return MediaStorageService(bucket="media", client_factory=lambda: client, cleanup_queue=queue)


@pytest.mark.parametrize("fmt, expected", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_real_images_are_accepted(media, fmt, expected):
    assert detect_image_type(_image(fmt)) == expected
    assert media.validate(_image(fmt)) == expected


@pytest.mark.parametrize("data", [b"GIF89a....", b"<svg onload=alert(1)>", b"\x89PNG\r\n\x1a\nnot really"])
def test_other_or_broken_files_are_422(media, data):
    with pytest.raises(AdminValidationError):
        media.validate(data)


def test_empty_file_is_422(media):
    with pytest.raises(AdminValidationError):
        media.validate(b"")


def test_too_large_file_is_413(storage, queue, monkeypatch):
    monkeypatch.setenv("ADMIN_MAX_UPLOAD_BYTES", "100")
    service = MediaStorageService(bucket="media", client_factory=MagicMock(), cleanup_queue=queue)

    with pytest.raises(AdminPayloadTooLarge):
        service.validate(_image(size=(200, 200)))


def test_too_many_pixels_is_422(media):
    with pytest.raises(AdminValidationError):
        media.validate(_image(size=(4001, 1)))


# ---------- media: upload, URLs, deletion ----------

def test_upload_uses_a_server_generated_name_in_a_safe_folder(media, storage):
    stored = media.upload("../Promo Group!", _image())

    path = storage.upload.call_args.args[0]
    assert path.startswith("promo-group/") and path.endswith(".png")
    assert stored.path == path
    assert stored.url == f"{BUCKET_URL}{path}"
    assert storage.upload.call_args.args[2]["upsert"] == "false"


def test_upload_failure_is_502(media, storage):
    storage.upload.side_effect = RuntimeError("storage down")

    with pytest.raises(AdminUpstreamError):
        media.upload("banners", _image())


@pytest.mark.parametrize("url, expected", [
    (f"{BUCKET_URL}tags/a%20b.png", "tags/a b.png"),
    ("https://dummy.supabase.co/storage/v1/object/public/banners-web/x.png", None),
    ("https://elsewhere.example/x.png", None),
    (None, None),
])
def test_path_from_url_only_accepts_this_bucket(media, url, expected):
    assert media.path_from_url(url) == expected


def test_remove_after_commit_deletes_each_path_once(media, storage, queue):
    media.remove_after_commit(["a.png", None, "a.png", "b.png"], reason="banner image replaced")

    storage.remove.assert_called_once_with(["a.png", "b.png"])
    queue.enqueue.assert_not_called()


def test_failed_delete_is_queued_not_raised(media, storage, queue):
    storage.remove.side_effect = RuntimeError("storage down")

    media.remove_staged(["a.png", "b.png"])

    assert [c.args for c in queue.enqueue.call_args_list] == [
        ("media", "a.png", "rollback of a failed save"), ("media", "b.png", "rollback of a failed save")]


def test_failure_to_queue_is_logged_not_raised(media, storage, queue):
    storage.remove.side_effect = RuntimeError("storage down")
    queue.enqueue.side_effect = RuntimeError("db down")

    media.remove_after_commit(["a.png"], reason="banner deleted")
