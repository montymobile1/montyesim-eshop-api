"""TEOS-292: two admins replacing the same tag's icon at the same time (PUT /groups/{id}, ``icon_<tag id>``).

Before: both requests read icon A, both uploaded, both removed A after their commit; the second write
replaced the first one's icon, whose file was never referenced nor removed (one orphan per round).
Now every edited tag carries ``expected_icon`` (the icon read before the upload); admin_edit_tag_group,
holding the group lock, refuses with AD409 ``guard_failed`` when it changed meanwhile, and the loser's
staged upload is removed. The SQL cannot run here: an in-memory repo applies the same rule, and the SQL is
checked as text. Storage is a fake bucket behind the real MediaStorageService, so "no orphan" is checked as
"every stored file is referenced by a tag".
"""
import copy
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image
from postgrest.exceptions import APIError

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminConflict
from app.repo.admin.base import AuditContext
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.tag_repo import TagAdminRepo
from app.schemas.admin.groups import AdminGroupUpdate
from app.services.admin.group_service import GroupAdminService
from app.services.admin.media_storage_service import MediaStorageService
from tests.admin.conftest import make_admin

ROOT = Path(__file__).resolve().parents[3]
PUBLIC = "https://x.supabase.co/storage/v1/object/public/media/"
CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4")
T1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OLD = "countries/original.png"


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (2, 2)).save(out, format="PNG")
    return out.getvalue()


class FakeBucket:
    def __init__(self, paths):
        self.files = set(paths)

    def upload(self, path, data, options):
        self.files.add(path)

    def remove(self, paths):
        self.files.difference_update(paths)

    def get_public_url(self, path):
        return PUBLIC + path


class FakeGroupRepo:
    """admin_edit_tag_group as the SQL does it: group lock, expected_icon guard, deleted_tags returned.
    ``between`` runs once, after the request read the group and before its write (the other admin)."""

    def __init__(self, tags, return_deleted=True):
        self.tags = {tag["id"]: dict(tag) for tag in tags}
        self.return_deleted = return_deleted
        self.between = None

    def name_taken(self, name, exclude_id=None):
        return False

    def bundle_counts(self, group_ids):
        return {}

    def get_with_tags(self, group_id, tag_search=None):
        return {"id": group_id, "name": "Countries", "type": 1, "group_category": "on_land",
                "tag": copy.deepcopy(list(self.tags.values()))}

    def edit_tag_group(self, group_id, group, new_rows, updated_rows, deleted_ids, ctx, rotate_cache=True):
        if self.between is not None:
            hook, self.between = self.between, None
            hook()
        stale = [{"id": row["id"], "expected": row["expected_icon"], "actual": self.tags[row["id"]]["icon"]}
                 for row in updated_rows
                 if "expected_icon" in row and self.tags[row["id"]]["icon"] != row["expected_icon"]]
        if stale:
            raise AdminConflict("guard_failed", table="tag", id=str(group_id), tags=stale)
        deleted = [{"id": tag_id, "icon": self.tags.pop(tag_id)["icon"]} for tag_id in deleted_ids]
        for row in updated_rows:
            self.tags[row["id"]].update(name=row["name"], icon=row["icon"])
        result = {"group": {"id": group_id, "name": group["name"]}, "tags": list(self.tags.values())}
        if self.return_deleted:
            result["deleted_tags"] = deleted
        return result


def _tag(tag_id, icon):
    return {"id": tag_id, "tag_group_id": 7, "name": f"tag {tag_id[:4]}", "icon": icon, "sorting_number": 1,
            "data": {}}


def _payload(updated=(), deleted=()):
    return AdminGroupUpdate.model_validate({
        "name": "Countries", "type": 1, "group_category": "on_land", "new_tags": [],
        "updated_tags": [{"id": tag_id, "name": f"tag {tag_id[:4]}"} for tag_id in updated],
        "deleted_tag_ids": list(deleted)})


def _setup(tags, files, **repo_kwargs):
    bucket = FakeBucket(files)
    client = MagicMock()
    client.storage.from_.return_value = bucket
    queue = MagicMock()
    media = MediaStorageService(bucket="media", client_factory=lambda: client, cleanup_queue=queue)
    repo = FakeGroupRepo(tags, **repo_kwargs)
    svc = GroupAdminService(repo=repo, tags=MagicMock(spec=TagAdminRepo), media=media)
    return svc, repo, bucket, queue


def _referenced(repo):
    return {tag["icon"][len(PUBLIC):] for tag in repo.tags.values() if tag.get("icon")}


# ---------- the race ----------

def test_two_admins_replacing_the_same_icon_second_gets_409_and_leaves_no_orphan():
    svc, repo, bucket, queue = _setup([_tag(T1, PUBLIC + OLD)], [OLD])
    # admin 1 saves while admin 2 is between its read (icon OLD) and its write
    repo.between = lambda: svc.update(7, _payload(updated=[T1]), {T1: _png()}, CTX)

    with pytest.raises(AdminConflict) as err:
        svc.update(7, _payload(updated=[T1]), {T1: _png()}, CTX)

    assert err.value.conflict_code == "guard_failed"
    detail = json.loads(err.value.details)
    assert detail["tags"][0]["id"] == T1 and detail["tags"][0]["expected"] == PUBLIC + OLD
    winner = repo.tags[T1]["icon"]
    assert winner != PUBLIC + OLD and detail["tags"][0]["actual"] == winner
    # admin 1's file is the one stored, OLD was removed once, admin 2's upload was removed: no orphan
    assert bucket.files == _referenced(repo) == {winner[len(PUBLIC):]}
    queue.enqueue.assert_not_called()


def test_rounds_of_concurrent_replacements_never_accumulate_files():
    svc, repo, bucket, _ = _setup([_tag(T1, PUBLIC + OLD)], [OLD])
    for _ in range(3):
        repo.between = lambda: svc.update(7, _payload(updated=[T1]), {T1: _png()}, CTX)
        with pytest.raises(AdminConflict):
            svc.update(7, _payload(updated=[T1]), {T1: _png()}, CTX)
    assert len(bucket.files) == 1 and bucket.files == _referenced(repo)


def test_name_only_edit_after_a_concurrent_icon_change_is_refused_not_pointed_at_the_removed_file():
    """Without the guard the stale edit wrote OLD back (already removed) and orphaned the new icon."""
    svc, repo, bucket, _ = _setup([_tag(T1, PUBLIC + OLD)], [OLD])
    repo.between = lambda: svc.update(7, _payload(updated=[T1]), {T1: _png()}, CTX)

    with pytest.raises(AdminConflict) as err:
        svc.update(7, _payload(updated=[T1]), {}, CTX)

    assert err.value.conflict_code == "guard_failed"
    assert bucket.files == _referenced(repo) and repo.tags[T1]["icon"] != PUBLIC + OLD


def test_tag_deleted_after_another_admin_replaced_its_icon_removes_the_new_icon():
    """The icons of deleted tags come from the database (deleted_tags), not from the stale read."""
    svc, repo, bucket, _ = _setup([_tag(T1, PUBLIC + OLD), _tag(T2, None)], [OLD])
    repo.between = lambda: svc.update(7, _payload(updated=[T1]), {T1: _png()}, CTX)

    svc.update(7, _payload(updated=[T2], deleted=[T1]), {}, CTX)

    assert T1 not in repo.tags and bucket.files == set()


# ---------- the normal flows still work ----------

def test_single_replacement_removes_the_old_file():
    svc, repo, bucket, queue = _setup([_tag(T1, PUBLIC + OLD)], [OLD])

    svc.update(7, _payload(updated=[T1]), {T1: _png()}, CTX)

    assert OLD not in bucket.files and bucket.files == _referenced(repo) and len(bucket.files) == 1
    queue.enqueue.assert_not_called()


def test_deleted_tag_icon_is_removed_with_an_old_database_function_too():
    """API deployed before the SQL: no deleted_tags in the result, the icons read before the edit are used."""
    svc, repo, bucket, _ = _setup([_tag(T1, PUBLIC + OLD)], [OLD], return_deleted=False)

    svc.update(7, _payload(deleted=[T1]), {}, CTX)

    assert bucket.files == set()


def test_every_edited_tag_sends_the_icon_it_was_read_with():
    repo = MagicMock(spec=TagGroupAdminRepo)
    repo.name_taken.return_value = False
    repo.bundle_counts.return_value = {}
    repo.get_with_tags.return_value = {"id": 7, "name": "Countries", "type": 1, "group_category": "on_land",
                                       "tag": [_tag(T1, PUBLIC + OLD), _tag(T2, None)]}
    repo.edit_tag_group.return_value = {"group": {"id": 7}, "tags": []}
    svc = GroupAdminService(repo=repo, tags=MagicMock(spec=TagAdminRepo), media=MagicMock())

    svc.update(7, _payload(updated=[T1, T2]), {}, CTX)

    rows = repo.edit_tag_group.call_args.args[3]
    assert [(row["id"], row["expected_icon"]) for row in rows] == [(T1, PUBLIC + OLD), (T2, None)]


# ---------- repository and route ----------

def test_repo_maps_the_sql_guard_failed_to_409():
    detail = {"code": "guard_failed", "table": "tag", "id": "7",
              "tags": [{"id": T1, "expected": PUBLIC + OLD, "actual": PUBLIC + "countries/b.png"}]}
    repo = TagGroupAdminRepo.__new__(TagGroupAdminRepo)
    repo.table_name = "tag_group"
    repo.client = MagicMock()
    repo.client.rpc.return_value.execute.side_effect = APIError(
        {"code": "AD409", "message": "guard_failed", "details": json.dumps(detail), "hint": None})

    with pytest.raises(AdminConflict) as err:
        repo.edit_tag_group(7, {"name": "Countries", "type": 1, "group_category": "on_land"}, [], [], [], CTX)

    assert err.value.conflict_code == "guard_failed"
    assert json.loads(err.value.details) == detail


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def test_put_group_answers_409_guard_failed(api_client, monkeypatch, as_admin):
    from app.api.v1.admin import groups as routes
    fake = MagicMock(name="group-service")
    fake.read_image.side_effect = lambda file: file.read()
    fake.update.side_effect = AdminConflict("guard_failed", table="tag", id="7", tags=[{"id": T1}])
    monkeypatch.setattr(routes, "service", fake)
    payload = {"name": "Countries", "type": 1, "group_category": "on_land",
               "updated_tags": [{"id": T1, "name": "x"}]}

    response = api_client.put("/api/v1/admin/groups/7", data={"payload": json.dumps(payload)},
                              files={f"icon_{T1}": ("a.png", _png(), "image/png")})

    assert response.status_code == 409
    assert json.loads(response.json()["developerMessage"])["code"] == "guard_failed"


# ---------- SQL (text) ----------

def _ddl(name):
    return (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def _edit_function(text, last=False):
    marker = "CREATE OR REPLACE FUNCTION public.admin_edit_tag_group("
    start = text.rindex(marker) if last else text.index(marker, text.index("-- ADMIN API (MS-5017)"))
    return text[start:text.index("\n$$;\n", start) + len("\n$$;")]


def test_sql_guards_expected_icon_under_the_lock_before_any_write():
    body = _edit_function(_ddl("supabase_ddl.sql"))
    guard = body.index("WHERE t.value ? 'expected_icon'")
    assert body.index("public.admin_lock_group(p_id)") < guard < body.index("UPDATE public.tag_group")
    assert "AND tg.icon IS DISTINCT FROM (t.value ->> 'expected_icon')" in body
    assert "MESSAGE = 'guard_failed'" in body
    assert "RETURN v_new || jsonb_build_object('deleted_tags', v_deleted_rows);" in body
    # the deleted icons are read before the delete
    assert body.index("INTO v_deleted_rows") < body.index("DELETE FROM public.tag WHERE id = ANY (v_deleted)")


@pytest.mark.parametrize("name", ["migrations/admin/new_change_ddl.sql", "migrations/admin/today_ddl_2026-10-30.sql"])
def test_lead_sql_files_hold_the_same_function(name):
    if not (ROOT / name).exists():
        pytest.skip(f"{name} is local only (gitignored)")
    text = _ddl(name)
    assert _edit_function(text, last=True) == _edit_function(_ddl("supabase_ddl.sql"))
    signature = "admin_edit_tag_group(bigint, text, integer, text, jsonb, jsonb, uuid[], uuid, text, text, boolean)"
    assert f"GRANT EXECUTE ON FUNCTION public.{signature}\n    TO service_role;" in text[text.rindex("-- TEOS-292"):]
