"""BannerAdminService and BannerAdminRepo (MS-5058, MS-5060) with the database and storage mocked."""
import io
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError
from app.repo.admin.banner_repo import BannerAdminRepo
from app.repo.admin.base import AuditContext, Paged
from app.schemas.admin.common import PageParams
from app.services.admin.banner_service import IMAGE_FOLDER, BannerAdminService
from app.services.admin.media_storage_service import MediaStorageService, StoredMedia

CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4")
PUBLIC = "https://x.supabase.co/storage/v1/object/public/media/"
OLD_URL = PUBLIC + "banners-web/1111-Old%20title.png"
NEW = StoredMedia(path="banners-web/new.jpg", url=PUBLIC + "banners-web/new.jpg", content_type="image/jpeg", size=4)
FIELDS = {"title": "Refer", "description": "d", "platform": "web", "action": "CHAT"}


def _row(**extra):
    return {"id": 3, "title": "Refer", "description": "d", "image": OLD_URL, "action": "CHAT",
            "platform": "web", "is_active": True, "created_at": "2026-10-20T00:00:00", **extra}


@pytest.fixture
def repo():
    return MagicMock(spec=BannerAdminRepo)


@pytest.fixture
def media():
    real = MediaStorageService(bucket="media", client_factory=MagicMock())
    fake = MagicMock(spec=MediaStorageService)
    fake.max_bytes = 10
    fake.upload.return_value = NEW
    fake.path_from_url.side_effect = real.path_from_url  # the real URL parsing
    return fake


@pytest.fixture
def svc(repo, media):
    return BannerAdminService(repo=repo, media=media)


# ---------- reads ----------

def test_list_shapes_rows_with_image_url_and_facets(svc, repo):
    repo.search.return_value = Paged(rows=[_row(), _row(id=4, image=None, is_active=False)], total=2)
    repo.facets.return_value = {"all": 2, "active": 1, "inactive": 1}

    result = svc.list("ref", "all", PageParams())

    assert result.total_count == 2
    assert result.facets == {"all": 2, "active": 1, "inactive": 1}
    assert result.data[0]["image_url"] == OLD_URL and "image" not in result.data[0]
    assert result.data[1]["image_url"] is None and result.data[1]["is_active"] is False


def test_get_unknown_is_404(svc, repo):
    repo.get_or_404.side_effect = AdminNotFound("not_found")
    with pytest.raises(AdminNotFound):
        svc.get(9)


def test_read_image_stops_one_byte_over_the_limit(svc):
    assert svc.read_image(io.BytesIO(b"x" * 100)) == b"x" * 11


# ---------- create ----------

def test_create_uploads_then_inserts_with_the_public_url(svc, repo, media):
    repo.insert_audited.return_value = _row(image=NEW.url)

    result = svc.create(FIELDS, b"data", CTX)

    media.upload.assert_called_once_with(IMAGE_FOLDER, b"data")
    assert IMAGE_FOLDER == "banners-web"
    repo.insert_audited.assert_called_once_with({**FIELDS, "image": NEW.url}, CTX, rotate_cache=True)
    assert result["image_url"] == NEW.url
    media.remove_staged.assert_not_called()


def test_create_failure_removes_the_staged_file(svc, repo, media):
    repo.insert_audited.side_effect = AdminConflict("duplicate_title")

    with pytest.raises(AdminConflict):
        svc.create(FIELDS, b"data", CTX)

    media.remove_staged.assert_called_once_with([NEW.path])


def test_create_upload_failure_writes_nothing(svc, repo, media):
    media.upload.side_effect = AdminUpstreamError("STORAGE_ERROR")
    with pytest.raises(AdminUpstreamError):
        svc.create(FIELDS, b"data", CTX)
    repo.insert_audited.assert_not_called()


# ---------- update ----------

def test_update_without_image_keeps_it_and_touches_no_file(svc, repo, media):
    repo.get_or_404.return_value = _row()
    repo.update_audited.return_value = _row(description="new")

    result = svc.update(3, {**FIELDS, "description": "new"}, None, CTX)

    repo.update_audited.assert_called_once_with(3, {**FIELDS, "description": "new"}, CTX, rotate_cache=True,
                                                guard=None)
    assert result["image_url"] == OLD_URL
    media.upload.assert_not_called()
    media.remove_after_commit.assert_not_called()


def test_update_with_new_image_saves_the_new_url_and_removes_the_old_file(svc, repo, media):
    """MS-5062: the new URL is saved (not undefined). MS-5063: the old file is removed from bucket media."""
    repo.get_or_404.return_value = _row()
    repo.update_audited.return_value = _row(image=NEW.url)

    result = svc.update(3, FIELDS, b"data", CTX)

    patch = repo.update_audited.call_args.args[1]
    assert patch["image"] == NEW.url
    assert repo.update_audited.call_args.kwargs["guard"] == {"image": OLD_URL}
    assert result["image_url"] == NEW.url
    media.remove_after_commit.assert_called_once()
    assert media.remove_after_commit.call_args.args[0] == ["banners-web/1111-Old title.png"]
    media.remove_staged.assert_not_called()


def test_update_failure_removes_the_staged_file_and_keeps_the_old_one(svc, repo, media):
    repo.get_or_404.return_value = _row()
    repo.update_audited.side_effect = AdminUpstreamError("DATABASE_ERROR")

    with pytest.raises(AdminUpstreamError):
        svc.update(3, FIELDS, b"data", CTX)

    media.remove_staged.assert_called_once_with([NEW.path])
    media.remove_after_commit.assert_not_called()


def test_update_concurrent_image_change_is_409_and_staged_file_removed(svc, repo, media):
    repo.get_or_404.return_value = _row()
    repo.update_audited.side_effect = AdminConflict("guard_failed")

    with pytest.raises(AdminConflict):
        svc.update(3, FIELDS, b"data", CTX)

    media.remove_staged.assert_called_once_with([NEW.path])


def test_update_with_nothing_changed_returns_the_banner(svc, repo, media):
    repo.get_or_404.return_value = _row()
    repo.update_audited.side_effect = AdminConflict("no_change")

    assert svc.update(3, FIELDS, None, CTX)["title"] == "Refer"


def test_update_unknown_banner_uploads_nothing(svc, repo, media):
    repo.get_or_404.side_effect = AdminNotFound("not_found")
    with pytest.raises(AdminNotFound):
        svc.update(9, FIELDS, b"data", CTX)
    media.upload.assert_not_called()


def test_update_legacy_banner_without_image_gets_one(svc, repo, media):
    repo.get_or_404.return_value = _row(image=None)
    repo.update_audited.return_value = _row(image=NEW.url)

    svc.update(3, FIELDS, b"data", CTX)

    media.remove_after_commit.assert_not_called()


# ---------- delete ----------

def test_delete_removes_the_image_after_commit(svc, repo, media):
    repo.delete_audited.return_value = _row()

    svc.delete(3, CTX)

    repo.delete_audited.assert_called_once_with(3, CTX, rotate_cache=True)
    assert media.remove_after_commit.call_args.args[0] == ["banners-web/1111-Old title.png"]


@pytest.mark.parametrize("image", [None, "undefined", "https://elsewhere.example.com/a.png"])
def test_delete_without_a_media_image_makes_no_storage_call(svc, repo, media, image):
    repo.delete_audited.return_value = _row(image=image)
    svc.delete(3, CTX)
    media.remove_after_commit.assert_not_called()


def test_delete_missing_is_404_and_no_storage_call(svc, repo, media):
    repo.delete_audited.side_effect = AdminNotFound("not_found")
    with pytest.raises(AdminNotFound):
        svc.delete(3, CTX)
    media.remove_after_commit.assert_not_called()


# ---------- repository ----------

def test_repo_maps_title_index_to_duplicate_title():
    repo = BannerAdminRepo.__new__(BannerAdminRepo)
    repo.table_name = "banner"
    repo.client = MagicMock()
    repo.client.rpc.return_value.execute.side_effect = APIError({
        "code": "23505", "message": 'duplicate key value violates unique constraint "banner_title_lower_key"',
        "details": None, "hint": None})

    with pytest.raises(AdminConflict) as err:
        repo.insert_audited({"title": "A"}, CTX)
    assert err.value.conflict_code == "duplicate_title"


@pytest.mark.parametrize("message, details", [
    # TEOS-239: QA's unique lower(title) index has another name; the detail still names the column
    ('duplicate key value violates unique constraint "banner_title_ci_idx"',
     "Key (lower(title::text))=(spring sale) already exists."),
    ('duplicate key value violates unique constraint "banner_title_key"', "Key (title)=(Spring Sale) already exists."),
])
def test_repo_maps_any_title_unique_index_to_duplicate_title(message, details):
    repo = BannerAdminRepo.__new__(BannerAdminRepo)
    repo.table_name = "banner"
    repo.client = MagicMock()
    repo.client.rpc.return_value.execute.side_effect = APIError({
        "code": "23505", "message": message, "details": details, "hint": None})

    with pytest.raises(AdminConflict) as err:
        repo.insert_audited({"title": "Spring Sale"}, CTX)
    assert err.value.conflict_code == "duplicate_title"


def test_repo_search_and_facets_filters():
    repo = BannerAdminRepo.__new__(BannerAdminRepo)
    query = MagicMock()
    repo.q = MagicMock(return_value=query)
    repo.filters = MagicMock(return_value=query)
    repo.paged = MagicMock(return_value=Paged(rows=[], total=0))
    repo.count = MagicMock(side_effect=[0, 0])

    repo.search("50%", "active", PageParams())
    assert repo.facets("50%") == {"all": 0, "active": 0, "inactive": 0}

    repo.filters.assert_called_once_with(query, ilike={"title": "50%"}, eq={"is_active": True})
    assert repo.count.call_args_list[1].kwargs == {"ilike": {"title": "50%"}, "eq": {"is_active": True}}
