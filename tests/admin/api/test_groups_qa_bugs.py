"""QA bugs of epic TEOS-102 on /api/v1/admin/groups, HTTP side:
TEOS-174 (type / category filters), TEOS-175 (is_active must be a JSON boolean), TEOS-184 (group_category
values), TEOS-185 (tag names up to 60 characters), TEOS-187 (at most 200 files), TEOS-188 (request size)."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.schemas.admin.common import AdminPage
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/groups"
TAG_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GROUP = {"id": 7, "name": "Regions", "type": 1, "group_category": "on_land", "is_active": True,
         "created_at": None, "updated_at": None, "tag_count": 1}
DETAIL = {**GROUP, "tags": [], "top_countries_count": None}
PNG = ("icon.png", b"\x89PNG\r\n\x1a\nfake", "image/png")


def _create(**overrides):
    body = {"name": "Regions", "type": 1, "group_category": "on_land",
            "tags": [{"client_key": "t1", "name": "Middle East"}]}
    body.update(overrides)
    return {"payload": json.dumps(body)}


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import groups as routes
    fake = MagicMock(name="group-service")
    fake.read_image.side_effect = lambda file: file.read()
    fake.create.return_value = DETAIL
    fake.update.return_value = DETAIL
    monkeypatch.setattr(routes, "service", fake)
    return fake


@pytest.fixture
def as_admin():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_super():
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_SUPER_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


# ---------- TEOS-174: type and category filters ----------

def test_list_passes_type_and_group_category_to_the_service(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[GROUP], total_count=1, facets={"all": 1, "active": 1, "inactive": 0})

    response = api_client.get(BASE, params={"type": 1, "group_category": "at_sea"})

    assert response.status_code == 200, response.json()
    args, kwargs = service.list.call_args
    assert kwargs == {"type": 1, "group_category": "at_sea"}


@pytest.mark.parametrize("params", [{"type": 3}, {"type": "x"}, {"group_category": "foo"}])
def test_list_refuses_unknown_type_or_category_values(api_client, service, as_admin, params):
    assert api_client.get(BASE, params=params).status_code == 422
    service.list.assert_not_called()


# ---------- TEOS-175: strict boolean ----------

@pytest.mark.parametrize("value", ["false", "no", "on", "true", "yes", 0, 1, 0.0])
def test_group_status_refuses_a_non_boolean_is_active(api_client, service, as_admin, value):
    response = api_client.patch(f"{BASE}/7/status", json={"is_active": value})

    assert response.status_code == 422, response.json()
    service.set_status.assert_not_called()


def test_group_status_still_accepts_json_booleans(api_client, service, as_admin):
    service.set_status.return_value = {"id": 7, "is_active": False}

    assert api_client.patch(f"{BASE}/7/status", json={"is_active": False}).status_code == 200


def test_bundle_status_refuses_a_string_is_active_too(api_client, as_super, monkeypatch):
    from app.api.v1.admin import bundles as bundle_routes
    fake = MagicMock(name="bundle-service")
    monkeypatch.setattr(bundle_routes, "service", fake)

    response = api_client.patch("/api/v1/admin/bundles/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/status",
                                json={"is_active": "yes"})

    assert response.status_code == 422, response.json()
    assert not fake.method_calls


# ---------- TEOS-184: group_category values ----------

@pytest.mark.parametrize("category", ["foo", "", "ON_LAND"])
def test_create_refuses_an_unknown_group_category(api_client, service, as_admin, category):
    response = api_client.post(BASE, data=_create(group_category=category), files={"icon_t1": PNG})

    assert response.status_code == 422, response.json()
    service.create.assert_not_called()


@pytest.mark.parametrize("category", ["on_land", "at_sea"])
def test_create_accepts_both_categories(api_client, service, as_admin, category):
    assert api_client.post(BASE, data=_create(group_category=category)).status_code == 201


# ---------- TEOS-185: tag names up to 60 characters ----------

def test_a_tag_name_of_60_characters_is_accepted(api_client, service, as_admin):
    body = _create(tags=[{"client_key": "t1", "name": "y" * 60}])

    assert api_client.post(BASE, data=body).status_code == 201


def test_a_tag_name_of_61_characters_is_refused(api_client, service, as_admin):
    response = api_client.post(BASE, data=_create(tags=[{"client_key": "t1", "name": "y" * 61}]))

    assert response.status_code == 422, response.json()
    service.create.assert_not_called()


def test_an_updated_tag_name_of_61_characters_is_refused(api_client, service, as_admin):
    body = {"name": "Regions", "type": 1, "group_category": "on_land",
            "updated_tags": [{"id": TAG_ID, "name": "y" * 61}]}

    response = api_client.put(f"{BASE}/7", data={"payload": json.dumps(body)})

    assert response.status_code == 422, response.json()
    service.update.assert_not_called()


# ---------- TEOS-187 / TEOS-188: file count and request size ----------

def test_more_than_200_files_is_413_before_any_file_is_read(api_client, service, as_admin):
    tags = [{"client_key": f"k{i}", "name": f"t{i}"} for i in range(201)]
    files = [(f"icon_k{i}", PNG) for i in range(201)]

    response = api_client.post(BASE, data=_create(tags=tags), files=files)

    assert response.status_code == 413, response.json()
    service.read_image.assert_not_called()
    service.create.assert_not_called()


def test_200_files_are_accepted(api_client, service, as_admin):
    tags = [{"client_key": f"k{i}", "name": f"t{i}"} for i in range(200)]
    files = [(f"icon_k{i}", PNG) for i in range(200)]

    assert api_client.post(BASE, data=_create(tags=tags), files=files).status_code == 201


def test_a_body_over_the_form_limit_is_413_before_anything_is_saved(api_client, service, as_admin, monkeypatch):
    monkeypatch.setenv("ADMIN_MAX_GROUP_FORM_BYTES", "2000")
    big = ("icon.png", b"\x89PNG\r\n\x1a\n" + b"0" * 900, "image/png")
    tags = [{"client_key": f"k{i}", "name": f"t{i}"} for i in range(3)]
    files = [(f"icon_k{i}", big) for i in range(3)]

    response = api_client.post(BASE, data=_create(tags=tags), files=files)

    assert response.status_code == 413, response.json()
    service.create.assert_not_called()


def test_a_body_under_the_form_limit_is_accepted(api_client, service, as_admin, monkeypatch):
    monkeypatch.setenv("ADMIN_MAX_GROUP_FORM_BYTES", "100000")

    assert api_client.post(BASE, data=_create(), files={"icon_t1": PNG}).status_code == 201


# ---------- TEOS-188 retest: the body is read to the end before the 413 ----------

def _streamed_request(chunks, content_length=None):
    """A Request whose body arrives in ``chunks``; ``sent`` records each chunk the API pulled."""
    from starlette.requests import Request
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    sent = []
    messages = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                for index, chunk in enumerate(chunks)]

    async def receive():
        message = messages.pop(0)
        sent.append(message["body"])
        return message

    return Request({"type": "http", "method": "POST", "headers": headers}, receive), sent


@pytest.mark.parametrize("content_length", [None, 30, 5], ids=["no length", "true length", "length too small"])
def test_a_body_over_the_limit_is_drained_then_413(content_length):
    import asyncio
    from app.api.v1.admin.groups import _read_body
    from app.exceptions.admin import AdminPayloadTooLarge
    chunks = [b"x" * 10, b"x" * 10, b"x" * 10]
    request, sent = _streamed_request(chunks, content_length)

    with pytest.raises(AdminPayloadTooLarge):
        asyncio.run(_read_body(request, 25))
    assert sent == chunks  # every chunk was received before answering, so the proxy gets the 413


def test_a_declared_length_over_the_limit_still_drains_the_body():
    import asyncio
    from app.api.v1.admin.groups import _read_body
    from app.exceptions.admin import AdminPayloadTooLarge
    chunks = [b"x" * 10, b"x" * 10]
    request, sent = _streamed_request(chunks, content_length=10_000)

    with pytest.raises(AdminPayloadTooLarge):
        asyncio.run(_read_body(request, 25))
    assert sent == chunks


def test_a_body_at_the_limit_is_kept_for_the_form():
    import asyncio
    from app.api.v1.admin.groups import _read_body
    request, _ = _streamed_request([b"x" * 15, b"x" * 10], content_length=25)

    asyncio.run(_read_body(request, 25))
    assert request._body == b"x" * 25


def test_the_default_form_limit_is_50_mb(monkeypatch):
    from app.api.v1.admin.groups import _max_form_bytes
    monkeypatch.delenv("ADMIN_MAX_GROUP_FORM_BYTES", raising=False)
    assert _max_form_bytes() == 50 * 1024 * 1024


# ---------- TEOS-191: each 413 tells the admin which limit was hit ----------

def test_too_many_files_message_names_the_limit(api_client, service, as_admin):
    tags = [{"client_key": f"k{i}", "name": f"t{i}"} for i in range(201)]
    files = [(f"icon_k{i}", PNG) for i in range(201)]

    body = api_client.post(BASE, data=_create(tags=tags), files=files).json()

    assert body["title"] == "At most 200 new icons can be uploaded in one save."
    assert body["developerMessage"] == "too many files: at most 200"


def test_form_too_large_message_names_the_limit(api_client, service, as_admin, monkeypatch):
    monkeypatch.setenv("ADMIN_MAX_GROUP_FORM_BYTES", str(2 * 1024))
    big = ("icon.png", b"\x89PNG\r\n\x1a\n" + b"0" * 1500, "image/png")
    files = [(f"icon_k{i}", big) for i in range(2)]
    tags = [{"client_key": f"k{i}", "name": f"t{i}"} for i in range(2)]

    body = api_client.post(BASE, data=_create(tags=tags), files=files).json()

    assert body["title"] == ("The new icons are too large together (at most 2 KB in one save). "
                             "Save the group with fewer new icons, then add the rest.")
    assert body["developerMessage"] == "request body too large"


def test_the_413_message_is_translated(api_client, service, as_admin):
    tags = [{"client_key": f"k{i}", "name": f"t{i}"} for i in range(201)]
    files = [(f"icon_k{i}", PNG) for i in range(201)]

    body = api_client.post(BASE, data=_create(tags=tags), files=files, headers={"Accept-Language": "ar"}).json()

    assert body["title"] == "يمكن رفع 200 أيقونة جديدة كحد أقصى في كل حفظ."


def test_an_oversized_image_says_so_with_the_limit():
    from app.exceptions.admin import AdminPayloadTooLarge
    from app.services.admin.media_storage_service import MediaStorageService
    storage = MediaStorageService.__new__(MediaStorageService)
    storage.max_bytes = 1024 * 1024

    with pytest.raises(AdminPayloadTooLarge) as raised:
        storage.validate(b"0" * (1024 * 1024 + 1))
    assert raised.value.name == "The image is larger than 1 MB."
    assert raised.value.details == "file larger than 1048576 bytes"


@pytest.mark.parametrize("size, label", [(50 * 1024 * 1024, "50 MB"), (512 * 1024, "512 KB"), (900, "900 bytes")])
def test_size_label(size, label):
    from app.exceptions.admin import size_label
    assert size_label(size) == label
