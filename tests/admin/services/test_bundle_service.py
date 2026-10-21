"""BundleAdminService and BundleAdminRepo (MS-5083) with the database mocked."""
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.base import AuditContext, Paged
from app.repo.admin.bundle_repo import BundleAdminRepo, DETAIL_SELECT, LIST_SELECT
from app.schemas.admin.bundles import (
    AdminBundleBulkStatus,
    AdminBundleRename,
    AdminBundleStatusPatch,
    AdminBundleTagsSet,
)
from app.schemas.admin.common import PageParams
from app.services.admin.bundle_service import BundleAdminService

ACTOR = "11111111-1111-1111-1111-111111111111"
CTX = AuditContext(actor_id=ACTOR, ip="1.2.3.4", reason="r")
BID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TAG1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TAG2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _row(**extra):
    return {"id": BID, "bundle_name": "Europe", "is_active": True, "created_at": "2026-10-20T00:00:00",
            "updated_at": None, "data": {"bundle_info_code": "EU5"}, **extra}


@pytest.fixture
def repo():
    fake = MagicMock(spec=BundleAdminRepo)
    fake.needs_search_function.side_effect = BundleAdminRepo.needs_search_function
    return fake


@pytest.fixture
def svc(repo):
    return BundleAdminService(repo=repo)


# ---------- list ----------

def test_list_without_search_uses_plain_query_and_whole_table_counts(svc, repo):
    repo.list_plain.return_value = Paged(rows=[_row(), _row(id=TAG1, is_active=None, data="junk")], total=2)
    repo.facets.return_value = {"all": 2, "active": 1, "inactive": 1}
    page = PageParams(page=1, page_size=20)

    result = svc.list(None, None, "all", page)

    repo.list_plain.assert_called_once_with("all", page)
    repo.search.assert_not_called()
    repo.totals.assert_not_called()
    assert result.total_count == 2
    assert result.facets == {"all": 2, "active": 1, "inactive": 1}
    assert result.totals == {"total": 2, "active_count": 1, "inactive_count": 1}
    assert result.data[1]["is_active"] is False and result.data[1]["data"] is None


def test_list_blank_search_and_empty_tags_is_plain(svc, repo):
    repo.list_plain.return_value = Paged(rows=[], total=0)
    repo.facets.return_value = {"all": 0, "active": 0, "inactive": 0}

    svc.list("   ", [], "inactive", PageParams())

    repo.list_plain.assert_called_once()
    repo.search.assert_not_called()


def test_list_with_search_or_tags_uses_the_search_function(svc, repo):
    repo.search.return_value = {"items": [_row()], "total_count": 1, "facets": {"all": 1, "active": 1, "inactive": 0}}
    repo.totals.return_value = {"total": 10, "active_count": 7, "inactive_count": 3}
    page = PageParams(page=2, page_size=5)

    result = svc.list("eu", [TAG1], "active", page)

    repo.search.assert_called_once_with("eu", [TAG1], "active", page)
    repo.list_plain.assert_not_called()
    assert result.total_count == 1
    assert result.facets == {"all": 1, "active": 1, "inactive": 0}
    assert result.totals == {"total": 10, "active_count": 7, "inactive_count": 3}
    assert result.data[0]["id"] == BID


# ---------- detail ----------

def test_get_shapes_tags_with_their_group(svc, repo):
    repo.detail.return_value = _row(bundle_tag=[
        {"id": 3, "tag": {"id": TAG1, "name": "France", "icon": "f.png",
                          "tag_group": {"id": 2, "name": "Europe", "type": 1, "group_category": "country"}}},
        {"id": 4, "tag": None},  # dangling link
        {"id": 5, "tag": {"id": TAG2, "name": "Loose", "icon": None, "tag_group": None}},
    ])

    result = svc.get(BID)

    repo.detail.assert_called_once_with(BID)
    assert result["bundle_name"] == "Europe"
    assert [t["bundle_tag_id"] for t in result["tags"]] == [3, 5]
    assert result["tags"][0]["tag"]["group"] == {"id": 2, "name": "Europe", "type": 1, "group_category": "country"}
    assert result["tags"][1]["tag"]["group"] is None


def test_get_missing_is_404(svc, repo):
    repo.detail.return_value = None
    with pytest.raises(AdminNotFound):
        svc.get(BID)


# ---------- writes ----------

def test_rename_calls_repo_and_returns_id_and_name(svc, repo):
    repo.rename.return_value = _row(bundle_name="New")

    result = svc.rename(BID, AdminBundleRename(bundle_name=" New "), CTX)

    repo.rename.assert_called_once_with(BID, "New", CTX)
    assert result == {"id": BID, "bundle_name": "New"}


def test_set_status_returns_id_and_state(svc, repo):
    repo.set_status.return_value = {"id": BID, "is_active": False}

    result = svc.set_status(BID, AdminBundleStatusPatch(is_active=False, reason="r"), CTX)

    repo.set_status.assert_called_once_with(BID, False, CTX)
    assert result == {"id": BID, "is_active": False}


def test_set_status_no_change_propagates(svc, repo):
    repo.set_status.side_effect = AdminConflict("no_change")
    with pytest.raises(AdminConflict) as err:
        svc.set_status(BID, AdminBundleStatusPatch(is_active=True), CTX)
    assert err.value.conflict_code == "no_change"


def test_status_patch_requires_reason_to_deactivate():
    with pytest.raises(ValueError):
        AdminBundleStatusPatch(is_active=False)
    assert AdminBundleStatusPatch(is_active=True).reason is None
    with pytest.raises(ValueError):
        AdminBundleBulkStatus(is_active=False, reason=" ")
    assert AdminBundleBulkStatus(is_active=True).is_active is True


def test_set_all_status_shapes_result(svc, repo):
    repo.set_all_status.return_value = {"rows_affected": 2, "affected_bundle_ids": [BID, TAG1]}

    result = svc.set_all_status(AdminBundleBulkStatus(is_active=False, reason="down"), CTX)

    repo.set_all_status.assert_called_once_with(False, CTX)
    assert result == {"rows_affected": 2, "affected_bundle_ids": [BID, TAG1]}


def test_set_tags_dedupes_and_keeps_empty_set(svc, repo):
    repo.set_tags.return_value = {"id": BID, "tag_ids": [TAG1], "added": 1, "removed": 0}

    result = svc.set_tags(BID, AdminBundleTagsSet(tag_ids=[TAG1, TAG1]), CTX)
    repo.set_tags.assert_called_once_with(BID, [TAG1], CTX)
    assert result == {"id": BID, "tag_ids": [TAG1]}

    repo.set_tags.return_value = {"id": BID, "tag_ids": []}
    assert svc.set_tags(BID, AdminBundleTagsSet(tag_ids=[]), CTX) == {"id": BID, "tag_ids": []}
    assert repo.set_tags.call_args.args[1] == []


# ---------- repository ----------

def _repo_with_client():
    repo = BundleAdminRepo.__new__(BundleAdminRepo)
    repo.table_name = "bundle"
    repo.client = MagicMock()
    return repo


def test_repo_needs_search_function():
    assert BundleAdminRepo.needs_search_function(None, None) is False
    assert BundleAdminRepo.needs_search_function(" ", []) is False
    assert BundleAdminRepo.needs_search_function("x", None) is True
    assert BundleAdminRepo.needs_search_function(None, [TAG1]) is True


def test_repo_search_calls_the_rpc_with_one_based_page_and_escaped_text_untouched():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = {
        "items": [_row()], "total_count": 1, "facets": {"all": 1, "active": 1, "inactive": 0}}

    result = repo.search(" 50% ", [TAG1], "inactive", PageParams(page=3, page_size=25))

    name, params = repo.client.rpc.call_args.args
    assert name == "admin_search_bundles"
    assert params == {"p_search": "50%", "p_tag_ids": [TAG1], "p_is_active": False, "p_page": 3, "p_page_size": 25}
    assert result["total_count"] == 1 and result["items"][0]["id"] == BID


def test_repo_search_tolerates_an_empty_result():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = None

    result = repo.search("x", None, "all", PageParams())

    assert result == {"items": [], "total_count": 0, "facets": {"all": 0, "active": 0, "inactive": 0}}
    assert repo.client.rpc.call_args.args[1]["p_tag_ids"] is None
    assert repo.client.rpc.call_args.args[1]["p_is_active"] is None


def test_repo_list_plain_filters_status_with_is_true():
    repo = BundleAdminRepo.__new__(BundleAdminRepo)
    query = MagicMock(name="query")
    repo.q = MagicMock(return_value=query)
    repo.paged = MagicMock(return_value=Paged(rows=[], total=0))

    repo.list_plain("inactive", PageParams())
    repo.q.assert_called_with(LIST_SELECT)
    query.not_.is_.assert_called_once_with("is_active", "true")

    repo.list_plain("active", PageParams())
    query.is_.assert_called_once_with("is_active", "true")

    query.reset_mock()
    repo.list_plain("all", PageParams())
    query.is_.assert_not_called()
    query.not_.is_.assert_not_called()


def test_repo_facets_and_totals_are_head_counts():
    repo = BundleAdminRepo.__new__(BundleAdminRepo)
    repo.count = MagicMock(side_effect=[5, 3, 5, 3])

    assert repo.facets() == {"all": 5, "active": 3, "inactive": 2}
    assert repo.totals() == {"total": 5, "active_count": 3, "inactive_count": 2}
    assert repo.count.call_args_list[1].kwargs == {"eq": {"is_active": True}}


def test_repo_detail_embeds_tags_and_groups():
    repo = _repo_with_client()
    repo.table = MagicMock()
    repo.table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [_row()]

    assert repo.detail(BID)["id"] == BID
    repo.table.select.assert_called_once_with(DETAIL_SELECT)
    repo.table.select.return_value.eq.assert_called_once_with("id", BID)

    repo.table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    assert repo.detail(BID) is None


def test_repo_rename_is_audited_and_rotates_cache():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = _row(bundle_name="New")

    repo.rename(BID, "New", CTX)

    name, params = repo.client.rpc.call_args.args
    assert name == "admin_update_audited"
    assert params["p_table"] == "bundle" and params["p_id"] == BID
    assert params["p_patch"] == {"bundle_name": "New"}
    assert params["p_actor"] == ACTOR and params["p_ip"] == "1.2.3.4" and params["p_reason"] == "r"
    assert params["p_rotate_cache"] is True


def test_repo_set_status_calls_the_rpc():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = {"id": BID, "is_active": False}

    assert repo.set_status(BID, False, CTX) == {"id": BID, "is_active": False}
    name, params = repo.client.rpc.call_args.args
    assert name == "admin_set_bundle_status"
    assert params == {"p_id": BID, "p_is_active": False, "p_actor": ACTOR, "p_ip": "1.2.3.4", "p_reason": "r"}


def test_repo_set_all_status_calls_the_rpc():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = {"rows_affected": 1, "affected_bundle_ids": [BID]}

    repo.set_all_status(True, CTX)

    name, params = repo.client.rpc.call_args.args
    assert name == "admin_set_all_bundles_status"
    assert params == {"p_is_active": True, "p_actor": ACTOR, "p_ip": "1.2.3.4", "p_reason": "r"}


def test_repo_set_tags_calls_the_rpc_with_an_empty_array():
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.return_value.data = {"id": BID, "tag_ids": []}

    repo.set_tags(BID, [], CTX)

    name, params = repo.client.rpc.call_args.args
    assert name == "admin_set_bundle_tags"
    assert params == {"p_bundle_id": BID, "p_tag_ids": [], "p_actor": ACTOR, "p_ip": "1.2.3.4", "p_reason": "r"}


@pytest.mark.parametrize("code, message, details, expected", [
    ("AD404", "not_found", '{"code":"not_found"}', AdminNotFound),
    ("AD409", "no_change", '{"code":"no_change"}', AdminConflict),
    ("AD422", "unknown tag id(s): x", '{"code":"unknown_tag","tag_ids":["x"]}', AdminValidationError),
])
def test_repo_maps_sql_errors(code, message, details, expected):
    repo = _repo_with_client()
    repo.client.rpc.return_value.execute.side_effect = APIError(
        {"code": code, "message": message, "details": details, "hint": None})

    with pytest.raises(expected) as err:
        repo.set_tags(BID, [TAG1], CTX)
    if expected is AdminConflict:
        assert err.value.conflict_code == "no_change"
