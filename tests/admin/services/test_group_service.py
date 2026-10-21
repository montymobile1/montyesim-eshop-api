"""GroupAdminService, TagGroupAdminRepo and TagAdminRepo (MS-5085) with the database and storage mocked."""
import io
from unittest.mock import MagicMock, call

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError, AdminValidationError
from app.repo.admin.base import AuditContext, Paged
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.tag_repo import TagAdminRepo
from app.schemas.admin.common import PageParams
from app.schemas.admin.groups import AdminGroupCreate, AdminGroupUpdate, normalize_alternative_country
from app.services.admin.group_service import GroupAdminService, build_tag_data
from app.services.admin.media_storage_service import MediaStorageService, StoredMedia

CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4")
PUBLIC = "https://x.supabase.co/storage/v1/object/public/media/"
T1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
T3 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OTHER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
NEW = StoredMedia(path="regions/new.png", url=PUBLIC + "regions/new.png", content_type="image/png", size=4)
NEW2 = StoredMedia(path="regions/new2.png", url=PUBLIC + "regions/new2.png", content_type="image/png", size=4)


def _group(**extra):
    return {"id": 7, "name": "Regions", "type": 1, "group_category": "on_land", "is_active": True,
            "created_at": "2026-10-20T00:00:00", "updated_at": "2026-10-20T00:00:00", **extra}


def _tag(tag_id=T1, name="Middle East", icon=PUBLIC + "Regions/1111-Middle%20East.png", sorting_number=2, **extra):
    return {"id": tag_id, "tag_group_id": 7, "name": name, "icon": icon, "sorting_number": sorting_number,
            "data": {"region_code": "Middle%20East", "alternative_country": None}, "created_at": None,
            "updated_at": None, **extra}


@pytest.fixture
def repo():
    return MagicMock(spec=TagGroupAdminRepo)


@pytest.fixture
def tags():
    return MagicMock(spec=TagAdminRepo)


@pytest.fixture
def media():
    real = MediaStorageService(bucket="media", client_factory=MagicMock())
    fake = MagicMock(spec=MediaStorageService)
    fake.max_bytes = 10
    fake.upload.side_effect = [NEW, NEW2]
    fake.path_from_url.side_effect = real.path_from_url  # the real URL parsing
    return fake


@pytest.fixture
def svc(repo, tags, media):
    return GroupAdminService(repo=repo, tags=tags, media=media)


# ---------- tag data (MS-5066) ----------

def test_region_data_uses_the_raw_name_on_create_and_edit():
    created = build_tag_data("Regions", "Middle East", T1, NEW.url, ["me", "gulf"])
    edited = build_tag_data("regions", "Middle East", T1, NEW.url, "me,gulf", existing={"region_code": "Middle%20East"},
                            is_new=False)
    for data in (created, edited):
        assert data["region_code"] == "Middle_East"
        assert data["zone_name"] == data["region_name"] == "Middle East"
        assert data["guid"] == T1 and data["icon"] == NEW.url
        assert data["alternative_country"] == "me,gulf"
    assert "%20" not in str(created) and "%20" not in str(edited)


def test_country_data_is_built_for_new_countries_only():
    created = build_tag_data("Countries", "Saudi Arabia", T1, None, None)
    assert created == {"id": T1, "icon": None, "country": "Saudi Arabia", "iso3_code": "Saudi_Arabia",
                       "zone_name": "Unknown", "country_code": "Saudi_Arabia", "operator_list": None,
                       "alternative_country": None}
    synced = {"iso3_code": "SAU", "country_code": "SA", "country": "Saudi Arabia", "operator_list": ["x"]}
    edited = build_tag_data("Countries", "Saudi Arabia", T1, None, ["KSA"], existing=synced, is_new=False)
    assert edited == {**synced, "alternative_country": "KSA"}


def test_other_groups_only_get_alternative_country_and_extra_data():
    assert build_tag_data("Sports", "Ski", T1, None, [" a ", "", "b"], extra={"k": 1}) == {
        "k": 1, "alternative_country": "a,b"}
    assert normalize_alternative_country("") is None and normalize_alternative_country([]) is None


# ---------- reads ----------

def test_list_shapes_rows_with_tag_count_and_facets(svc, repo):
    repo.search.return_value = Paged(rows=[_group(tag=[{"count": 12}]), _group(id=8, is_active=False, tag=[])], total=2)
    repo.facets.return_value = {"all": 2, "active": 1, "inactive": 1}

    result = svc.list("reg", "all", PageParams())

    assert result.total_count == 2 and result.facets == {"all": 2, "active": 1, "inactive": 1}
    assert result.data[0]["tag_count"] == 12 and "tag" not in result.data[0]
    assert result.data[1]["tag_count"] == 0 and result.data[1]["is_active"] is False
    repo.search.assert_called_once_with("reg", "all", PageParams())


def test_get_orders_tags_and_adds_top_countries_count_for_countries(svc, repo):
    repo.get_with_tags.return_value = _group(name="Countries", tag=[_tag(T1, "Zed", sorting_number=1),
                                                                     _tag(T2, "Alpha", sorting_number=10000),
                                                                     _tag(T3, "Beta", sorting_number=None)])
    repo.top_countries_count.return_value = 12

    result = svc.get(7, "a")

    assert [tag["id"] for tag in result["tags"]] == [T1, T2, T3]
    assert result["top_countries_count"] == 12 and result["tag_count"] == 3
    repo.get_with_tags.assert_called_once_with(7, "a")


def test_get_other_group_has_no_top_countries_lookup(svc, repo):
    repo.get_with_tags.return_value = _group(tag=[])
    assert svc.get(7)["top_countries_count"] is None
    repo.top_countries_count.assert_not_called()


def test_get_unknown_is_404(svc, repo):
    repo.get_with_tags.return_value = None
    with pytest.raises(AdminNotFound):
        svc.get(9)


def test_tags_checks_the_group_then_lists(svc, repo, tags):
    repo.get_or_404.return_value = {"id": 7}
    tags.by_group.return_value = [_tag()]
    assert svc.tags(7)[0]["id"] == T1
    tags.by_group.assert_called_once_with(7)


def test_read_image_stops_one_byte_over_the_limit(svc):
    assert svc.read_image(io.BytesIO(b"x" * 100)) == b"x" * 11


# ---------- create ----------

def _create(**overrides):
    body = {"name": "Regions", "type": 1, "group_category": "on_land",
            "tags": [{"client_key": "a", "name": "Middle East", "alternative_country": ["me"]},
                     {"client_key": "b", "name": "Africa"}]}
    return AdminGroupCreate.model_validate({**body, **overrides})


def test_create_uploads_then_inserts_everything_in_one_call(svc, repo, media):
    repo.insert_group_with_tags.return_value = {"group": _group(), "tags": [_tag(T1, icon=NEW.url), _tag(T2, "Africa", None)]}

    result = svc.create(_create(), {"a": b"png"}, CTX)

    media.upload.assert_called_once_with("Regions", b"png")
    group, rows, ctx = repo.insert_group_with_tags.call_args.args
    assert group == {"name": "Regions", "type": 1, "group_category": "on_land"}
    assert repo.insert_group_with_tags.call_args.kwargs == {"rotate_cache": True}
    assert ctx is CTX
    assert rows[0]["name"] == "Middle East" and rows[0]["icon"] == NEW.url
    assert rows[0]["data"]["region_code"] == "Middle_East" and rows[0]["data"]["guid"] == rows[0]["id"]
    assert rows[0]["data"]["alternative_country"] == "me"
    assert rows[1]["icon"] is None and rows[1]["data"]["icon"] is None
    assert len({row["id"] for row in rows}) == 2
    assert result["id"] == 7 and len(result["tags"]) == 2
    media.remove_staged.assert_not_called()


def test_create_failure_removes_the_staged_files(svc, repo, media):
    repo.insert_group_with_tags.side_effect = AdminConflict("duplicate_group_name")

    with pytest.raises(AdminConflict):
        svc.create(_create(), {"a": b"1", "b": b"2"}, CTX)

    media.remove_staged.assert_called_once_with([NEW.path, NEW2.path])


def test_create_second_upload_failure_removes_the_first_file_and_writes_nothing(svc, repo, media):
    media.upload.side_effect = [NEW, AdminUpstreamError("STORAGE_ERROR")]

    with pytest.raises(AdminUpstreamError):
        svc.create(_create(), {"a": b"1", "b": b"2"}, CTX)

    repo.insert_group_with_tags.assert_not_called()
    media.remove_staged.assert_called_once_with([NEW.path])


def test_create_icon_for_unknown_tag_is_422_before_any_upload(svc, repo, media):
    with pytest.raises(AdminValidationError):
        svc.create(_create(), {"zz": b"1"}, CTX)
    media.upload.assert_not_called()
    repo.insert_group_with_tags.assert_not_called()


# ---------- update ----------

def _update(**overrides):
    body = {"name": "Regions", "type": 1, "group_category": "on_land", "new_tags": [], "updated_tags": [],
            "deleted_tag_ids": []}
    return AdminGroupUpdate.model_validate({**body, **overrides})


def test_update_replaces_removes_and_deletes_icons_after_commit(svc, repo, media):
    current = _group(tag=[_tag(T1), _tag(T2, "Africa", PUBLIC + "Regions/2-Africa.png"),
                          _tag(T3, "Asia", PUBLIC + "Regions/3-Asia.png")])
    repo.get_with_tags.return_value = current
    repo.edit_tag_group.return_value = {"group": _group(group_category="at_sea"), "tags": [_tag(T1, icon=NEW.url)]}
    payload = _update(group_category="at_sea",
                      new_tags=[{"client_key": "n", "name": "Europe"}],
                      updated_tags=[{"id": T1, "name": "Middle East"}, {"id": T2, "name": "Africa", "remove_icon": True}],
                      deleted_tag_ids=[T3])

    result = svc.update(7, payload, {T1.upper(): b"1", "n": b"2"}, CTX)

    assert media.upload.call_args_list == [call("Regions", b"1"), call("Regions", b"2")]
    group_id, group, new_rows, updated_rows, deleted, ctx = repo.edit_tag_group.call_args.args
    assert group_id == 7 and group["group_category"] == "at_sea" and deleted == [T3] and ctx is CTX
    assert repo.edit_tag_group.call_args.kwargs == {"rotate_cache": True}
    assert new_rows[0]["name"] == "Europe" and new_rows[0]["icon"] == NEW2.url
    assert updated_rows[0] == {"id": T1, "name": "Middle East", "icon": NEW.url,
                               "data": {"guid": T1, "icon": NEW.url, "zone_name": "Middle East",
                                        "region_code": "Middle_East", "region_name": "Middle East",
                                        "alternative_country": None}}
    assert updated_rows[1]["icon"] is None and updated_rows[1]["data"]["icon"] is None
    media.remove_staged.assert_not_called()
    removed = media.remove_after_commit.call_args.args[0]
    assert sorted(removed) == ["Regions/1111-Middle East.png", "Regions/2-Africa.png", "Regions/3-Asia.png"]
    assert result["group_category"] == "at_sea"


def test_update_keeps_the_icon_when_no_file_and_no_remove(svc, repo, media):
    repo.get_with_tags.return_value = _group(tag=[_tag(T1)])
    repo.edit_tag_group.return_value = {"group": _group(), "tags": [_tag(T1)]}

    svc.update(7, _update(updated_tags=[{"id": T1, "name": "Middle East"}]), {}, CTX)

    updated_rows = repo.edit_tag_group.call_args.args[3]
    assert updated_rows[0]["icon"] == _tag()["icon"]
    media.upload.assert_not_called()
    media.remove_after_commit.assert_not_called()


def test_update_failure_removes_staged_files_and_keeps_the_old_ones(svc, repo, media):
    repo.get_with_tags.return_value = _group(tag=[_tag(T1)])
    repo.edit_tag_group.side_effect = AdminUpstreamError("DATABASE_ERROR")

    with pytest.raises(AdminUpstreamError):
        svc.update(7, _update(updated_tags=[{"id": T1, "name": "ME"}]), {T1: b"1"}, CTX)

    media.remove_staged.assert_called_once_with([NEW.path])
    media.remove_after_commit.assert_not_called()


def test_update_unknown_group_uploads_nothing(svc, repo, media):
    repo.get_with_tags.return_value = None
    with pytest.raises(AdminNotFound):
        svc.update(9, _update(), {}, CTX)
    media.upload.assert_not_called()


@pytest.mark.parametrize("overrides, files", [
    ({"updated_tags": [{"id": OTHER, "name": "x"}]}, {}),
    ({"deleted_tag_ids": [OTHER]}, {}),
    ({"updated_tags": [{"id": T1, "name": "x"}], "deleted_tag_ids": [T1]}, {}),
    ({"updated_tags": [{"id": T1, "name": "x"}, {"id": T1, "name": "y"}]}, {}),
    ({}, {OTHER: b"1"}),
    ({"new_tags": [{"client_key": "n", "name": "x"}]}, {"m": b"1"}),
])
def test_update_rejects_tags_of_other_groups_and_unknown_icons(svc, repo, media, overrides, files):
    repo.get_with_tags.return_value = _group(tag=[_tag(T1)])
    with pytest.raises(AdminValidationError):
        svc.update(7, _update(**overrides), files, CTX)
    media.upload.assert_not_called()
    repo.edit_tag_group.assert_not_called()


# ---------- delete ----------

def test_delete_removes_the_icons_after_commit(svc, repo, media):
    repo.delete_group.return_value = {"group": _group(), "tags": [_tag(T1), _tag(T2, "Africa", None)]}

    svc.delete(7, CTX)

    repo.delete_group.assert_called_once_with(7, CTX, rotate_cache=True)
    assert media.remove_after_commit.call_args.args[0] == ["Regions/1111-Middle East.png"]


def test_delete_linked_group_is_409_and_no_icon_touched(svc, repo, media):
    """MS-5064: the usage check happens in the transaction; nothing is deleted from storage."""
    repo.delete_group.side_effect = AdminConflict("linked_bundles", bundle_count=3)
    with pytest.raises(AdminConflict) as err:
        svc.delete(7, CTX)
    assert err.value.extra == {"bundle_count": 3}
    media.remove_after_commit.assert_not_called()
    media.remove_staged.assert_not_called()


def test_delete_missing_is_404(svc, repo, media):
    repo.delete_group.side_effect = AdminNotFound("not_found")
    with pytest.raises(AdminNotFound):
        svc.delete(7, CTX)
    media.remove_after_commit.assert_not_called()


# ---------- status / reorder ----------

def test_set_status_is_an_audited_update_that_rotates_the_cache(svc, repo):
    repo.update_audited.return_value = _group(is_active=False)
    assert svc.set_status(7, False, CTX) == {"id": 7, "is_active": False}
    repo.update_audited.assert_called_once_with(7, {"is_active": False}, CTX, rotate_cache=True)


def test_reorder_passes_the_full_ordered_set(svc, repo):
    repo.reorder_tags.return_value = {"tag_ids": [T2, T1]}
    assert svc.reorder(7, [T2, T1], CTX) == {"tag_ids": [T2, T1]}
    repo.reorder_tags.assert_called_once_with(7, [T2, T1], CTX, rotate_cache=True)


# ---------- repositories ----------

def _rpc_repo(error=None, data=None):
    repo = TagGroupAdminRepo.__new__(TagGroupAdminRepo)
    repo.table_name = "tag_group"
    repo.client = MagicMock()
    execute = repo.client.rpc.return_value.execute
    if error is not None:
        execute.side_effect = error
    else:
        execute.return_value = MagicMock(data=data)
    return repo


@pytest.mark.parametrize("constraint, code", [("tag_group_name_key", "duplicate_group_name"),
                                              ("unique_tag_name_per_group", "duplicate_tag_name")])
def test_repo_maps_unique_constraints(constraint, code):
    repo = _rpc_repo(APIError({"code": "23505", "message": f'duplicate key value violates unique constraint "{constraint}"',
                               "details": None, "hint": None}))
    with pytest.raises(AdminConflict) as err:
        repo.insert_group_with_tags({"name": "A", "type": 1, "group_category": "x"}, [], CTX)
    assert err.value.conflict_code == code


def test_repo_maps_linked_bundles_with_bundle_count():
    repo = _rpc_repo(APIError({"code": "AD409", "message": "linked_bundles",
                               "details": '{"code": "linked_bundles", "bundle_count": 4}', "hint": None}))
    with pytest.raises(AdminConflict) as err:
        repo.delete_group(7, CTX)
    assert err.value.conflict_code == "linked_bundles" and err.value.extra == {"bundle_count": 4}


def test_repo_rpc_names_and_params():
    repo = _rpc_repo(data={"group": {}, "tags": []})
    ctx = AuditContext(actor_id="u", ip="9.9.9.9", reason="r")

    repo.insert_group_with_tags({"name": "A", "type": 2, "group_category": "at_sea"}, [{"name": "A"}], ctx)
    repo.edit_tag_group(7, {"name": "A", "type": 1, "group_category": "x"}, [], [{"id": T1}], [T2], ctx, rotate_cache=False)
    repo.delete_group(7, ctx)
    repo.reorder_tags(7, [T1], ctx)

    calls = repo.client.rpc.call_args_list
    assert calls[0].args == ("admin_insert_group_with_tags", {
        "p_name": "A", "p_type": 2, "p_group_category": "at_sea", "p_tags": [{"name": "A"}], "p_actor": "u",
        "p_ip": "9.9.9.9", "p_reason": "r", "p_rotate_cache": True})
    assert calls[1].args[0] == "admin_edit_tag_group"
    assert calls[1].args[1]["p_deleted_tag_ids"] == [T2] and calls[1].args[1]["p_rotate_cache"] is False
    assert calls[2].args == ("admin_delete_group", {"p_id": 7, "p_actor": "u", "p_ip": "9.9.9.9", "p_reason": "r",
                                                    "p_rotate_cache": True})
    assert calls[3].args == ("admin_reorder_tags", {"p_group_id": 7, "p_tag_ids": [T1], "p_actor": "u",
                                                    "p_ip": "9.9.9.9", "p_reason": "r", "p_rotate_cache": True})


def test_repo_search_embeds_the_tag_count_and_facets_filter():
    repo = TagGroupAdminRepo.__new__(TagGroupAdminRepo)
    query = MagicMock()
    repo.q = MagicMock(return_value=query)
    repo.filters = MagicMock(return_value=query)
    repo.paged = MagicMock(return_value=Paged(rows=[], total=0))
    repo.count = MagicMock(side_effect=[3, 1])

    repo.search("50%", "inactive", PageParams())
    assert repo.facets("50%") == {"all": 3, "active": 1, "inactive": 2}

    repo.q.assert_called_once_with("*, tag(count)")
    repo.filters.assert_called_once_with(query, ilike={"name": "50%"}, eq={"is_active": False})
    assert repo.count.call_args_list[1].kwargs == {"ilike": {"name": "50%"}, "eq": {"is_active": True}}


def test_repo_get_with_tags_orders_the_embed_and_filters_tag_names():
    repo = TagGroupAdminRepo.__new__(TagGroupAdminRepo)
    repo.table_name = "tag_group"
    query = MagicMock()
    query.eq.return_value = query
    query.ilike.return_value = query
    query.order.return_value = query
    query.limit.return_value = query
    query.execute.return_value = MagicMock(data=[_group(tag=[])])
    repo.table = MagicMock()
    repo.table.select.return_value = query

    assert repo.get_with_tags(7, "a_b")["id"] == 7

    repo.table.select.assert_called_once_with("*, tag(*)")
    query.ilike.assert_called_once_with("tag.name", "%a\\_b%")
    assert query.order.call_args_list == [call("sorting_number", desc=False, foreign_table="tag"),
                                          call("name", desc=False, foreign_table="tag")]


def test_repo_top_countries_count_reads_app_config():
    repo = TagGroupAdminRepo.__new__(TagGroupAdminRepo)
    repo.table_name = "tag_group"
    repo.client = MagicMock()
    chain = repo.client.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute.return_value = MagicMock(data=[{"value": " 15 "}])
    assert repo.top_countries_count() == 15
    repo.client.table.assert_called_once_with("app_config")
    chain.execute.return_value = MagicMock(data=[{"value": "many"}])
    assert repo.top_countries_count() is None
    chain.execute.return_value = MagicMock(data=[])
    assert repo.top_countries_count() is None


def test_tag_repo_by_group_orders_by_sorting_number_then_name():
    repo = TagAdminRepo.__new__(TagAdminRepo)
    repo.table_name = "tag"
    query = MagicMock()
    query.eq.return_value = query
    query.order.return_value = query
    query.execute.return_value = MagicMock(data=[_tag()])
    repo.table = MagicMock()
    repo.table.select.return_value = query

    assert repo.by_group(7)[0]["id"] == T1
    query.eq.assert_called_once_with("tag_group_id", 7)
    assert query.order.call_args_list == [call("sorting_number"), call("name")]
