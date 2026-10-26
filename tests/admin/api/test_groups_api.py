"""HTTP contract of /api/v1/admin/groups (MS-5085)."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminPayloadTooLarge, AdminValidationError
from app.schemas.admin.common import AdminPage
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/groups"
TAG_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TAG_ID_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

GROUP = {"id": 7, "name": "Regions", "type": 1, "group_category": "on_land", "is_active": True,
         "created_at": "2026-10-20T10:00:00", "updated_at": "2026-10-20T10:00:00", "tag_count": 1}
TAG = {"id": TAG_ID, "tag_group_id": 7, "name": "Middle East", "icon": None, "sorting_number": 1,
       "data": {"region_code": "Middle_East"}, "created_at": None, "updated_at": None}
DETAIL = {**GROUP, "tags": [TAG], "top_countries_count": None}
CREATE = {"name": "Regions", "type": 1, "group_category": "on_land",
          "tags": [{"client_key": "t1", "name": "Middle East", "alternative_country": ["ME"]}]}
UPDATE = {"name": "Regions", "type": 1, "group_category": "on_land", "new_tags": [],
          "updated_tags": [{"id": TAG_ID, "name": "Middle East", "remove_icon": True}], "deleted_tag_ids": []}
PNG = ("icon.png", b"\x89PNG\r\n\x1a\nfake", "image/png")


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import groups as routes
    fake = MagicMock(name="group-service")
    fake.read_image.side_effect = lambda file: file.read()
    monkeypatch.setattr(routes, "service", fake)
    return fake


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_reader():
    """An admin holding only admin.groups.read."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="22222222-2222-2222-2222-222222222222", email="reader@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.groups.read"}), token="t")
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def _failed(response, status):
    body = response.json()
    assert response.status_code == status, body
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    return body


def _code(response):
    return json.loads(response.json()["developerMessage"])["code"]


def _payload(body):
    return {"payload": json.dumps(body)}


# ---------- list ----------

def test_list_returns_rows_with_tag_count_and_facets(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[GROUP], total_count=1, facets={"all": 1, "active": 1, "inactive": 0})

    response = api_client.get(BASE, params={"search": "reg", "status": "active", "page_size": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["data"][0]["tag_count"] == 1
    assert body["data"]["facets"] == {"all": 1, "active": 1, "inactive": 0}
    assert body["totalCount"] == 1
    search, status, page = service.list.call_args.args
    assert (search, status, page.page, page.page_size) == ("reg", "active", 1, 10)


@pytest.mark.parametrize("params", [{"page_size": 101}, {"status": "foo"}, {"name": "x"}, {"page": 0}])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)
    service.list.assert_not_called()


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)


def test_list_without_read_permission_is_403(api_client, service):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="3", email="x@example.com", role=ROLE_ADMIN, permissions=frozenset(), token="t")
    try:
        _failed(api_client.get(BASE), 403)
    finally:
        esim_app.dependency_overrides.pop(admin_user, None)


def test_lookup_route_still_answers_before_the_id_route(api_client, as_admin, monkeypatch):
    from app.services.admin import lookup_service
    fake = MagicMock()
    fake.groups.return_value = AdminPage(data=[], total_count=0)
    monkeypatch.setattr(lookup_service, "service", fake)

    response = api_client.get(f"{BASE}/lookup", params={"search": "x"})

    assert response.status_code == 200
    fake.groups.assert_called_once()


# ---------- detail / tags ----------

def test_get_returns_group_with_ordered_tags(api_client, service, as_admin):
    service.get.return_value = DETAIL
    response = api_client.get(f"{BASE}/7", params={"tag_search": "mid"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tags"][0]["id"] == TAG_ID and data["tags"][0]["data"]["region_code"] == "Middle_East"
    assert data["top_countries_count"] is None
    service.get.assert_called_once_with(7, "mid")


def test_get_unknown_is_404_and_malformed_is_422(api_client, service, as_admin):
    service.get.side_effect = AdminNotFound("not_found")
    _failed(api_client.get(f"{BASE}/999"), 404)
    _failed(api_client.get(f"{BASE}/abc"), 422)
    _failed(api_client.get(f"{BASE}/7", params={"foo": "x"}), 422)


def test_group_tags_lists_every_tag(api_client, service, as_admin):
    service.tags.return_value = [TAG]
    response = api_client.get(f"{BASE}/7/tags")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["total_count"] == 1 and body["data"]["data"][0]["name"] == "Middle East"
    service.tags.assert_called_once_with(7)


def test_group_tags_unknown_group_is_404(api_client, service, as_admin):
    service.tags.side_effect = AdminNotFound("not_found")
    _failed(api_client.get(f"{BASE}/999/tags"), 404)


# ---------- create ----------

def test_create_multipart_returns_201_with_payload_and_icons(api_client, service, as_admin):
    service.create.return_value = DETAIL

    response = api_client.post(BASE, data=_payload(CREATE), files={"icon_t1": PNG},
                               headers={"X-Forwarded-For": "8.8.8.8"})

    assert response.status_code == 201
    assert response.json()["responseCode"] == 201
    assert response.json()["data"]["tags"][0]["id"] == TAG_ID
    payload, files, ctx = service.create.call_args.args
    assert payload.name == "Regions" and payload.type == 1 and payload.tags[0].client_key == "t1"
    assert payload.tags[0].alternative_country == ["ME"]
    assert files == {"t1": PNG[1]}
    assert ctx.ip == "8.8.8.8"


def test_create_without_icons_is_allowed(api_client, service, as_admin):
    service.create.return_value = DETAIL
    response = api_client.post(BASE, data=_payload(CREATE))
    assert response.status_code == 201
    assert service.create.call_args.args[1] == {}


def test_create_without_payload_is_422(api_client, service, as_admin):
    _failed(api_client.post(BASE, files={"icon_t1": PNG}), 422)
    service.create.assert_not_called()


def test_create_payload_not_json_is_422(api_client, service, as_admin):
    _failed(api_client.post(BASE, data={"payload": "{not json"}), 422)
    service.create.assert_not_called()


@pytest.mark.parametrize("override", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 30}, {"type": 3}, {"type": "flat"}, {"group_category": ""},
    {"tags": []}, {"tags": [{"client_key": "t1", "name": ""}]}, {"tags": [{"client_key": "bad key", "name": "A"}]},
    {"tags": [{"client_key": "t1", "name": "A"}, {"client_key": "t1", "name": "B"}]},
    {"tags": [{"client_key": "t1", "name": "A", "icon": "x"}]}, {"link": "x"},
])
def test_create_invalid_payload_is_422(api_client, service, as_admin, override):
    _failed(api_client.post(BASE, data=_payload({**CREATE, **override})), 422)
    service.create.assert_not_called()


def test_create_unknown_form_field_is_422(api_client, service, as_admin):
    _failed(api_client.post(BASE, data={**_payload(CREATE), "name": "Regions"}), 422)
    _failed(api_client.post(BASE, data=_payload(CREATE), files={"image": PNG}), 422)
    _failed(api_client.post(BASE, data=_payload(CREATE), files={"icon_": PNG}), 422)
    service.create.assert_not_called()


def test_create_icon_for_unknown_tag_is_422(api_client, service, as_admin):
    service.create.side_effect = AdminValidationError("icon for unknown tag(s): zz")
    _failed(api_client.post(BASE, data=_payload(CREATE), files={"icon_zz": PNG}), 422)


@pytest.mark.parametrize("code", ["duplicate_group_name", "duplicate_tag_name"])
def test_create_duplicate_is_409(api_client, service, as_admin, code):
    service.create.side_effect = AdminConflict(code)
    response = api_client.post(BASE, data=_payload(CREATE))
    _failed(response, 409)
    assert _code(response) == code


def test_create_too_large_icon_is_413(api_client, service, as_admin):
    service.create.side_effect = AdminPayloadTooLarge("file larger than 1048576 bytes")
    _failed(api_client.post(BASE, data=_payload(CREATE), files={"icon_t1": PNG}), 413)


def test_create_read_only_admin_is_403_and_no_file_read(api_client, service, as_reader):
    _failed(api_client.post(BASE, data=_payload(CREATE), files={"icon_t1": PNG}), 403)
    service.create.assert_not_called()
    service.read_image.assert_not_called()


def test_create_without_session_is_401(api_client, service):
    _failed(api_client.post(BASE, data=_payload(CREATE)), 401)


# ---------- update ----------

def test_update_sends_payload_and_icon_of_an_existing_tag(api_client, service, as_admin):
    service.update.return_value = DETAIL
    body = {**UPDATE, "new_tags": [{"client_key": "n1", "name": "Africa"}], "deleted_tag_ids": [TAG_ID_2]}

    response = api_client.put(f"{BASE}/7", data=_payload(body), files={f"icon_{TAG_ID}": PNG})

    assert response.status_code == 200
    group_id, payload, files, _ctx = service.update.call_args.args
    assert group_id == 7
    assert payload.updated_tags[0].remove_icon is True and payload.new_tags[0].client_key == "n1"
    assert payload.deleted_tag_ids == [TAG_ID_2]
    assert files == {TAG_ID: PNG[1]}


def test_update_defaults_tag_lists_to_empty(api_client, service, as_admin):
    service.update.return_value = DETAIL
    response = api_client.put(f"{BASE}/7", data=_payload({"name": "Regions", "type": 2, "group_category": "at_sea"}))
    assert response.status_code == 200
    payload = service.update.call_args.args[1]
    assert payload.new_tags == [] and payload.updated_tags == [] and payload.deleted_tag_ids == []


@pytest.mark.parametrize("override", [
    {"updated_tags": [{"id": "not-a-uuid", "name": "A"}]}, {"deleted_tag_ids": ["nope"]},
    {"deleted_tag_ids": [TAG_ID, TAG_ID]}, {"updated_tags": [{"id": TAG_ID, "name": "A", "icon": "x"}]},
    {"type": 0},
])
def test_update_invalid_payload_is_422(api_client, service, as_admin, override):
    _failed(api_client.put(f"{BASE}/7", data=_payload({**UPDATE, **override})), 422)
    service.update.assert_not_called()


def test_update_unknown_is_404(api_client, service, as_admin):
    service.update.side_effect = AdminNotFound("not_found")
    _failed(api_client.put(f"{BASE}/999", data=_payload(UPDATE)), 404)


def test_update_tag_of_another_group_is_422(api_client, service, as_admin):
    service.update.side_effect = AdminValidationError("tag_not_in_group: " + TAG_ID_2)
    _failed(api_client.put(f"{BASE}/7", data=_payload(UPDATE)), 422)


def test_update_deleting_a_linked_tag_is_409_with_bundle_count(api_client, service, as_admin):
    service.update.side_effect = AdminConflict("linked_bundles", bundle_count=3)
    response = api_client.put(f"{BASE}/7", data=_payload(UPDATE))
    _failed(response, 409)
    assert json.loads(response.json()["developerMessage"]) == {"code": "linked_bundles", "bundle_count": 3}


def test_update_read_only_admin_is_403(api_client, service, as_reader):
    _failed(api_client.put(f"{BASE}/7", data=_payload(UPDATE)), 403)
    service.update.assert_not_called()


# ---------- delete ----------

def test_delete_is_204(api_client, service, as_admin):
    response = api_client.delete(f"{BASE}/7")
    assert response.status_code == 204
    assert response.content == b""
    assert service.delete.call_args.args[0] == 7


def test_delete_twice_second_is_404(api_client, service, as_admin):
    service.delete.side_effect = [None, AdminNotFound("not_found")]
    assert api_client.delete(f"{BASE}/7").status_code == 204
    _failed(api_client.delete(f"{BASE}/7"), 404)


def test_delete_linked_group_is_409_with_bundle_count(api_client, service, as_admin):
    service.delete.side_effect = AdminConflict("linked_bundles", bundle_count=2)
    response = api_client.delete(f"{BASE}/7")
    _failed(response, 409)
    assert json.loads(response.json()["developerMessage"]) == {"code": "linked_bundles", "bundle_count": 2}


def test_delete_read_only_admin_is_403(api_client, service, as_reader):
    _failed(api_client.delete(f"{BASE}/7"), 403)
    service.delete.assert_not_called()


# ---------- status ----------

def test_status_patch_passes_reason_in_the_audit_context(api_client, service, as_admin):
    service.set_status.return_value = {"id": 7, "is_active": False}

    response = api_client.patch(f"{BASE}/7/status", json={"is_active": False, "reason": "season over"})

    assert response.status_code == 200
    assert response.json()["data"] == {"id": 7, "is_active": False}
    group_id, is_active, ctx = service.set_status.call_args.args
    assert (group_id, is_active, ctx.reason) == (7, False, "season over")


def test_status_same_state_is_409_no_change(api_client, service, as_admin):
    service.set_status.side_effect = AdminConflict("no_change")
    response = api_client.patch(f"{BASE}/7/status", json={"is_active": True})
    _failed(response, 409)
    assert _code(response) == "no_change"


@pytest.mark.parametrize("body", [{}, {"is_active": "maybe"}, {"is_active": True, "extra": 1}])
def test_status_bad_body_is_422(api_client, service, as_admin, body):
    _failed(api_client.patch(f"{BASE}/7/status", json=body), 422)
    service.set_status.assert_not_called()


def test_status_unknown_is_404(api_client, service, as_admin):
    service.set_status.side_effect = AdminNotFound("not_found")
    _failed(api_client.patch(f"{BASE}/999/status", json={"is_active": False}), 404)


# ---------- reorder ----------

def test_reorder_sends_the_ordered_ids(api_client, service, as_admin):
    service.reorder.return_value = {"tag_ids": [TAG_ID_2, TAG_ID]}

    response = api_client.put(f"{BASE}/7/tags/order", json={"tag_ids": [TAG_ID_2, TAG_ID]})

    assert response.status_code == 200
    assert response.json()["data"] == {"tag_ids": [TAG_ID_2, TAG_ID]}
    group_id, tag_ids, _ctx = service.reorder.call_args.args
    assert (group_id, tag_ids) == (7, [TAG_ID_2, TAG_ID])


# {"tag_ids": []} is valid since TEOS-198 (a group without tags): see test_groups_qa_bugs_teos.py
@pytest.mark.parametrize("body", [{"tag_ids": ["x"]}, {"tag_ids": [TAG_ID, TAG_ID]},
                                  {"tag_ids": [TAG_ID], "extra": 1}, {}])
def test_reorder_bad_body_is_422(api_client, service, as_admin, body):
    _failed(api_client.put(f"{BASE}/7/tags/order", json=body), 422)
    service.reorder.assert_not_called()


def test_reorder_tag_of_another_group_is_422(api_client, service, as_admin):
    service.reorder.side_effect = AdminValidationError("tag_not_in_group")
    _failed(api_client.put(f"{BASE}/7/tags/order", json={"tag_ids": [TAG_ID]}), 422)


def test_reorder_read_only_admin_is_403(api_client, service, as_reader):
    _failed(api_client.put(f"{BASE}/7/tags/order", json={"tag_ids": [TAG_ID]}), 403)
    service.reorder.assert_not_called()
