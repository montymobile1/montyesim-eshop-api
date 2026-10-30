"""Group QA bugs of 2026-10-26: TEOS-168, TEOS-173, TEOS-176, TEOS-196, TEOS-197, TEOS-198.

The SQL functions cannot run here (no database): their Python side and error mapping are tested, and
supabase_ddl.sql / new_change_ddl.sql are checked as text (the fix is in the function body, and the
copy in new_change_ddl.sql is identical to the one in supabase_ddl.sql). new_change_ddl.sql lives in the
gitignored migrations/admin/ folder (handed to the lead outside git): its checks are skipped where it is absent.
"""
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import admin_user
from app.exceptions.admin import AdminConflict, AdminValidationError
from app.repo.admin.base import AuditContext, Paged
from app.repo.admin.bundle_repo import BundleAdminRepo
from app.repo.admin.tag_group_repo import TagGroupAdminRepo
from app.repo.admin.tag_repo import TagAdminRepo
from app.schemas.admin.common import PageParams
from app.schemas.admin.groups import AdminGroup, AdminTagOrder
from app.services.admin.group_service import GroupAdminService
from tests.admin.conftest import make_admin

ROOT = Path(__file__).resolve().parents[3]
NEW_CHANGE_DDL = "migrations/admin/new_change_ddl.sql"
needs_new_change_ddl = pytest.mark.skipif(not (ROOT / NEW_CHANGE_DDL).exists(),
                                          reason=f"{NEW_CHANGE_DDL} is local only (gitignored)")
CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4")
T1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


# ---------- helpers ----------

def _rpc_repo(cls, table, error=None, data=None):
    repo = cls.__new__(cls)
    repo.table_name = table
    repo.client = MagicMock()
    execute = repo.client.rpc.return_value.execute
    if error is not None:
        execute.side_effect = error
    else:
        execute.return_value = MagicMock(data=data)
    return repo


def _api_error(code, message, detail):
    return APIError({"code": code, "message": message, "details": json.dumps(detail), "hint": None})


def _ddl(name):
    return (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def _function(text, name, last=False):
    """The function's text; ``last``: its last copy (a file run top to bottom keeps that one, TEOS-292)."""
    marker = f"CREATE OR REPLACE FUNCTION public.{name}("
    start = text.rindex(marker) if last else text.index(marker)
    return text[start:text.index("\n$$;\n", start) + len("\n$$;")]


def _admin_section():
    text = _ddl("supabase_ddl.sql")
    return text[text.index("-- ADMIN API (MS-5017)"):]


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def group_routes_service(monkeypatch):
    from app.api.v1.admin import groups as routes
    fake = MagicMock(name="group-service")
    monkeypatch.setattr(routes, "service", fake)
    return fake


@pytest.fixture
def bundle_routes_service(monkeypatch):
    from app.api.v1.admin import bundles as routes
    fake = MagicMock(name="bundle-service")
    monkeypatch.setattr(routes, "service", fake)
    return fake


@pytest.fixture
def repo():
    mock = MagicMock(spec=TagGroupAdminRepo)
    mock.name_taken.return_value = False
    mock.bundle_counts.return_value = {}
    return mock


@pytest.fixture
def svc(repo):
    return GroupAdminService(repo=repo, tags=MagicMock(spec=TagAdminRepo), media=MagicMock())


def _group(**extra):
    return {"id": 7, "name": "Regions", "type": 1, "group_category": "on_land", "is_active": True,
            "created_at": None, "updated_at": None, **extra}


# ---------- DDL: both copies identical ----------

GROUP_OBJECTS = ["admin_set_bundle_tags", "admin_edit_tag_group", "admin_delete_group", "admin_reorder_tags",
                 "admin_group_bundle_counts"]


SIGNATURES = [
    "admin_set_bundle_tags(uuid, uuid[], uuid, text, text)",
    "admin_edit_tag_group(bigint, text, integer, text, jsonb, jsonb, uuid[], uuid, text, text, boolean)",
    "admin_delete_group(bigint, uuid, text, text, boolean)",
    "admin_reorder_tags(bigint, uuid[], uuid, text, text, boolean)",
    "admin_group_bundle_counts(bigint[])",
]
INDEX_LINE = "CREATE INDEX IF NOT EXISTS bundle_tag_tag_id_idx ON public.bundle_tag (tag_id);"


@needs_new_change_ddl
@pytest.mark.parametrize("name", GROUP_OBJECTS)
def test_new_change_ddl_holds_the_same_function_as_supabase_ddl(name):
    assert _function(_ddl(NEW_CHANGE_DDL), name, last=True) == _function(_admin_section(), name)


@pytest.mark.parametrize("signature", SIGNATURES)
def test_supabase_ddl_keeps_the_grants(signature):
    text = _admin_section()
    assert f"REVOKE ALL ON FUNCTION public.{signature}" in text
    assert f"GRANT EXECUTE ON FUNCTION public.{signature}\n    TO service_role;" in text


@needs_new_change_ddl
@pytest.mark.parametrize("signature", SIGNATURES)
def test_new_change_ddl_keeps_the_grants(signature):
    text = _ddl(NEW_CHANGE_DDL)
    assert f"REVOKE ALL ON FUNCTION public.{signature}" in text
    assert f"GRANT EXECUTE ON FUNCTION public.{signature}\n    TO service_role;" in text


def test_bundle_tag_tag_id_index_is_in_supabase_ddl():
    assert INDEX_LINE in _admin_section()


@needs_new_change_ddl
def test_bundle_tag_tag_id_index_is_in_new_change_ddl():
    assert INDEX_LINE in _ddl(NEW_CHANGE_DDL)


@needs_new_change_ddl
@pytest.mark.parametrize("header", ["-- TEOS-168:", "-- TEOS-196, TEOS-176:", "-- TEOS-176:",
                                    "-- TEOS-197, TEOS-198:", "-- TEOS-173:"])
def test_new_change_ddl_has_a_header_per_change(header):
    assert any(line.startswith(header) for line in _ddl(NEW_CHANGE_DDL).splitlines())


# ---------- TEOS-168: inactive group tag on PUT /bundles/{id}/tags ----------

def test_set_bundle_tags_sql_refuses_every_tag_of_an_inactive_group_before_any_write():
    body = _function(_admin_section(), "admin_set_bundle_tags")
    check = body.index("'inactive_group_tag'")
    query = body[body.index("INTO v_inactive"):body.index("IF v_inactive IS NOT NULL")]
    assert "WHERE tg.is_active IS FALSE;" in query
    # follow-up: tags the bundle already has are refused too (no "only newly added" exception)
    assert "bundle_tag" not in query
    assert body.index("'unknown_tag'") < check < body.index("DELETE FROM public.bundle_tag")
    assert "ERRCODE = 'AD422'" in body[body.rindex("RAISE EXCEPTION", 0, check):check]


def test_set_bundle_tags_already_linked_inactive_tag_is_a_422_and_nothing_is_saved():
    """The bundle already has T1 (group switched off since): saving [T1, T2] is refused with T1 named."""
    repo = _rpc_repo(BundleAdminRepo, "bundle", error=_api_error(
        "AD422", f"tag(s) of an inactive group: {T1}", {"code": "inactive_group_tag", "tag_ids": [T1]}))
    with pytest.raises(AdminValidationError) as err:
        repo.set_tags(BID, [T1, T2], CTX)
    assert json.loads(err.value.details) == {"code": "inactive_group_tag", "tag_ids": [T1]}
    repo.client.rpc.assert_called_once()
    assert repo.client.rpc.call_args.args[1]["p_tag_ids"] == [T1, T2]


# ---------- TEOS-168 follow-up: is_active of the groups the assign page reads ----------

def test_groups_lookup_selects_and_returns_is_active():
    from app.services.admin.lookup_service import LookupService, group_item
    assert "is_active" in TagGroupAdminRepo.lookup.__doc__
    assert group_item({"id": 7, "name": "Regions", "type": 1, "group_category": "region",
                       "is_active": False}).model_dump()["is_active"] is False
    assert group_item({"id": 8, "name": "Seas", "is_active": True}).model_dump()["is_active"] is True
    groups = MagicMock(spec=TagGroupAdminRepo)
    groups.lookup.return_value = Paged(rows=[{"id": 7, "name": "Regions", "type": 1, "group_category": "region",
                                              "is_active": False}], total=1)
    svc = LookupService(groups=groups, users=MagicMock(), bundles=MagicMock(), tags=MagicMock(),
                        promotion_rules=MagicMock())
    assert svc.groups(None, PageParams()).data[0].model_dump() == {
        "id": "7", "label": "Regions", "type": 1, "group_category": "region", "is_active": False}


def test_groups_lookup_api_carries_is_active(api_client, as_admin, monkeypatch):
    from app.schemas.admin.common import AdminPage, LookupItem
    from app.services.admin import lookup_service
    fake = MagicMock()
    fake.groups.return_value = AdminPage[LookupItem](data=[LookupItem(
        id="7", label="Regions", type=1, group_category="region", is_active=False)], total_count=1)
    monkeypatch.setattr(lookup_service, "service", fake)

    response = api_client.get("/api/v1/admin/groups/lookup")

    assert response.status_code == 200
    assert response.json()["data"]["data"][0]["is_active"] is False


def test_bundle_detail_select_embeds_the_group_is_active():
    from app.repo.admin.bundle_repo import DETAIL_SELECT
    assert "tag_group(id,name,type,group_category,is_active)" in DETAIL_SELECT


@pytest.mark.parametrize("stored, shown", [(False, False), (True, True), (None, True)])
def test_bundle_detail_groups_carry_is_active(stored, shown):
    from app.services.admin.bundle_service import BundleAdminService
    tags = BundleAdminService._shape_tags([{"id": 3, "tag": {"id": T1, "name": "Old", "icon": None, "tag_group": {
        "id": 5, "name": "Legacy", "type": 1, "group_category": "country", "is_active": stored}}}])
    assert tags[0]["tag"]["group"]["is_active"] is shown


def test_get_bundle_api_returns_the_group_is_active(api_client, bundle_routes_service, as_admin):
    bundle_routes_service.get.return_value = {
        "id": BID, "bundle_name": "Europe", "is_active": True, "data": {}, "tags": [{"bundle_tag_id": 3, "tag": {
            "id": T1, "name": "Old", "icon": None,
            "group": {"id": 5, "name": "Legacy", "type": 1, "group_category": "country", "is_active": False}}}]}

    response = api_client.get(f"/api/v1/admin/bundles/{BID}")

    assert response.status_code == 200
    assert response.json()["data"]["tags"][0]["tag"]["group"]["is_active"] is False


def test_bundle_repo_maps_inactive_group_tag_to_a_json_422():
    repo = _rpc_repo(BundleAdminRepo, "bundle", error=_api_error(
        "AD422", f"tag(s) of an inactive group: {T1}", {"code": "inactive_group_tag", "tag_ids": [T1]}))
    with pytest.raises(AdminValidationError) as err:
        repo.set_tags(BID, [T1], CTX)
    assert err.value.code == 422
    assert json.loads(err.value.details) == {"code": "inactive_group_tag", "tag_ids": [T1]}


def test_unknown_tag_keeps_its_text_422():
    repo = _rpc_repo(BundleAdminRepo, "bundle", error=_api_error(
        "AD422", f"unknown tag id(s): {T1}", {"code": "unknown_tag", "tag_ids": [T1]}))
    with pytest.raises(AdminValidationError) as err:
        repo.set_tags(BID, [T1], CTX)
    assert err.value.details == f"unknown tag id(s): {T1}"


def test_other_repos_keep_text_422_for_the_same_code():
    repo = _rpc_repo(TagGroupAdminRepo, "tag_group", error=_api_error(
        "AD422", "incomplete_order", {"code": "incomplete_order", "missing_tag_ids": [T1]}))
    with pytest.raises(AdminValidationError) as err:
        repo.reorder_tags(7, [], CTX)
    assert err.value.details == "incomplete_order"


def test_put_bundle_tags_inactive_group_tag_body(api_client, bundle_routes_service, as_admin):
    bundle_routes_service.set_tags.side_effect = AdminValidationError(
        json.dumps({"code": "inactive_group_tag", "tag_ids": [T1]}))

    response = api_client.put(f"/api/v1/admin/bundles/{BID}/tags", json={"tag_ids": [T1]})

    assert response.status_code == 422
    assert json.loads(response.json()["developerMessage"]) == {"code": "inactive_group_tag", "tag_ids": [T1]}


# ---------- TEOS-173: bundle_count on the groups list and detail ----------

def test_bundle_counts_sql_counts_distinct_bundles_in_one_grouped_query():
    body = _function(_admin_section(), "admin_group_bundle_counts")
    assert "count(DISTINCT bt.bundle_id)" in body and "GROUP BY t.tag_group_id" in body
    assert "t.tag_group_id = ANY" in body


def test_repo_bundle_counts_is_one_rpc_and_fills_missing_groups_with_zero():
    repo = _rpc_repo(TagGroupAdminRepo, "tag_group", data=[{"group_id": 7, "bundle_count": 3}])

    assert repo.bundle_counts([7, 8, 7]) == {7: 3, 8: 0}
    repo.client.rpc.assert_called_once_with("admin_group_bundle_counts", {"p_group_ids": [7, 8]})


def test_repo_bundle_counts_without_ids_makes_no_call():
    repo = _rpc_repo(TagGroupAdminRepo, "tag_group", data=[])
    assert repo.bundle_counts([]) == {}
    repo.client.rpc.assert_not_called()


def test_list_rows_carry_bundle_count_from_one_call(svc, repo):
    repo.search.return_value = Paged(rows=[_group(tag=[{"count": 2}]), _group(id=8, tag=[])], total=2)
    repo.facets.return_value = {"all": 2, "active": 2, "inactive": 0}
    repo.bundle_counts.return_value = {7: 1, 8: 0}

    result = svc.list(None, "all", PageParams())

    assert [(row["id"], row["tag_count"], row["bundle_count"]) for row in result.data] == [(7, 2, 1), (8, 0, 0)]
    repo.bundle_counts.assert_called_once_with([7, 8])


def test_get_carries_bundle_count(svc, repo):
    repo.get_with_tags.return_value = _group(tag=[])
    repo.bundle_counts.return_value = {7: 5}
    assert svc.get(7)["bundle_count"] == 5
    repo.bundle_counts.assert_called_once_with([7])


def test_admin_group_model_has_bundle_count():
    assert AdminGroup(id=1).bundle_count == 0
    assert AdminGroup(id=1, bundle_count=4).model_dump()["bundle_count"] == 4


def test_get_groups_api_returns_bundle_count(api_client, group_routes_service, as_admin):
    from app.schemas.admin.common import AdminPage
    group_routes_service.list.return_value = AdminPage(data=[{**_group(), "tag_count": 2, "bundle_count": 1}],
                                                       total_count=1, facets={"all": 1, "active": 1, "inactive": 0})

    response = api_client.get("/api/v1/admin/groups")

    assert response.status_code == 200
    row = response.json()["data"]["data"][0]
    assert row["bundle_count"] == 1 and row["tag_count"] == 2


# ---------- TEOS-176: delete 409 counts distinct bundles ----------

def test_delete_group_sql_counts_distinct_bundles():
    body = _function(_admin_section(), "admin_delete_group")
    assert "SELECT count(DISTINCT bt.bundle_id) INTO v_bundle_count" in body
    assert "count(*) INTO v_bundle_count" not in body


def test_delete_409_passes_the_distinct_count_through():
    """One bundle carrying two tags of the group: the database answers 1, the API passes 1 on."""
    repo = _rpc_repo(TagGroupAdminRepo, "tag_group", error=_api_error(
        "AD409", "linked_bundles", {"code": "linked_bundles", "bundle_count": 1}))
    with pytest.raises(AdminConflict) as err:
        repo.delete_group(7, CTX)
    assert json.loads(err.value.details) == {"code": "linked_bundles", "bundle_count": 1}


# ---------- TEOS-196: edit 409 names the linked tags ----------

def test_edit_group_sql_names_the_linked_deleted_tags():
    body = _function(_admin_section(), "admin_edit_tag_group")
    assert "SELECT count(DISTINCT bt.bundle_id) INTO v_bundle_count" in body
    assert "'tag_ids', coalesce(v_linked_ids, '[]'::jsonb)" in body
    assert "'tags', coalesce(v_linked, '[]'::jsonb)" in body
    assert "jsonb_build_object('id', l.id::text, 'name', l.name, 'bundle_count', l.bundle_count)" in body
    # refused before any write
    assert body.index("'linked_bundles'") < body.index("UPDATE public.tag_group")


def test_edit_409_carries_tag_ids_and_names():
    detail = {"code": "linked_bundles", "bundle_count": 2, "tag_ids": [T1],
              "tags": [{"id": T1, "name": "qa128 a3", "bundle_count": 2}]}
    repo = _rpc_repo(TagGroupAdminRepo, "tag_group", error=_api_error("AD409", "linked_bundles", detail))
    with pytest.raises(AdminConflict) as err:
        repo.edit_tag_group(7, {"name": "A", "type": 1, "group_category": "on_land"}, [], [], [T1], CTX)
    assert err.value.conflict_code == "linked_bundles"
    assert json.loads(err.value.details) == detail


def test_put_group_409_body_names_the_tags(api_client, group_routes_service, as_admin):
    tags = [{"id": T1, "name": "qa128 a3", "bundle_count": 2}]
    group_routes_service.update.side_effect = AdminConflict("linked_bundles", bundle_count=2, tag_ids=[T1], tags=tags)
    payload = {"name": "Regions", "type": 1, "group_category": "on_land", "deleted_tag_ids": [T1]}

    response = api_client.put("/api/v1/admin/groups/7", data={"payload": json.dumps(payload)})

    assert response.status_code == 409
    assert json.loads(response.json()["developerMessage"]) == {"code": "linked_bundles", "bundle_count": 2,
                                                               "tag_ids": [T1], "tags": tags}


# ---------- TEOS-197 / TEOS-198: reorder no-op and empty list ----------

def test_reorder_sql_is_a_no_op_when_the_order_is_already_stored():
    body = _function(_admin_section(), "admin_reorder_tags")
    no_op = body.index("WHERE t.sorting_number IS DISTINCT FROM o.n::integer")
    early_return = body.index("RETURN jsonb_build_object('tag_ids', to_jsonb(p_tag_ids));", no_op)
    # the no-op return comes after every check and before the update, the rotation and the audit row
    assert body.index("'incomplete_order'") < no_op
    assert early_return < body.index("UPDATE public.tag AS t")
    assert early_return < body.index("admin_rotate_cache_key()")
    assert early_return < body.index("admin_write_audit(")


def test_reorder_sql_accepts_an_empty_list_but_still_requires_every_tag():
    body = _function(_admin_section(), "admin_reorder_tags")
    assert "cardinality(p_tag_ids) = 0" not in body
    assert re.search(r"IF p_tag_ids IS NULL THEN", body)
    # incomplete_order is still raised for [] on a group with tags (t.id <> ALL('{}') is true for every tag)
    assert "t.id <> ALL (p_tag_ids)" in body


def test_tag_order_schema_accepts_an_empty_list():
    assert AdminTagOrder(tag_ids=[]).tag_ids == []


def test_reorder_empty_group_returns_an_empty_order(svc, repo):
    repo.reorder_tags.return_value = {"tag_ids": []}
    assert svc.reorder(7, [], CTX) == {"tag_ids": []}
    repo.reorder_tags.assert_called_once_with(7, [], CTX, rotate_cache=True)


def test_put_tags_order_with_an_empty_list_is_200(api_client, group_routes_service, as_admin):
    group_routes_service.reorder.return_value = {"tag_ids": []}

    response = api_client.put("/api/v1/admin/groups/7/tags/order", json={"tag_ids": []})

    assert response.status_code == 200 and response.json()["data"] == {"tag_ids": []}
    assert group_routes_service.reorder.call_args.args[:2] == (7, [])


def test_put_tags_order_same_order_is_200_with_the_order(api_client, group_routes_service, as_admin):
    group_routes_service.reorder.return_value = {"tag_ids": [T2, T1]}
    response = api_client.put("/api/v1/admin/groups/7/tags/order", json={"tag_ids": [T2, T1]})
    assert response.status_code == 200 and response.json()["data"] == {"tag_ids": [T2, T1]}
