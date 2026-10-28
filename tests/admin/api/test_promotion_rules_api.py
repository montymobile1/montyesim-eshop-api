"""HTTP contract of /api/v1/admin/promotion-rules (MS-5087). The lookup route (MS-5077) has its own tests."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError, AdminValidationError
from app.schemas.admin.common import AdminPage
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/promotion-rules"
RULE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

ROW = {"id": RULE_ID, "id_text": RULE_ID, "name": "DISCOUNT_AMOUNT-CREATE_ORDER-3", "max_usage": 3,
       "beneficiary": 1, "created_at": "2026-10-20T10:00:00",
       "action": {"id": 1, "name": "DISCOUNT_AMOUNT"}, "event": {"id": 1, "name": "CREATE_ORDER"}}
BODY = {"promotion_rule_action_id": 1, "promotion_rule_event_id": 1, "max_usage": 3, "beneficiary": 1}


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import promotion_rules as routes
    fake = MagicMock(name="promotion-rule-service")
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
    """An admin holding only admin.rules.read."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="22222222-2222-2222-2222-222222222222", email="reader@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.rules.read"}), token="t")
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def _failed(response, status):
    body = response.json()
    assert response.status_code == status
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    return body


def _code(response):
    return json.loads(response.json()["developerMessage"])["code"]


# ---------- list, actions, events ----------

def test_list_returns_rules_with_action_and_event(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[ROW], total_count=1)

    response = api_client.get(BASE, params={"search": "aaaa", "action_id": 1, "event_id": 2, "page": 2,
                                            "page_size": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 1
    assert body["data"]["data"][0]["action"] == {"id": 1, "name": "DISCOUNT_AMOUNT"}
    assert body["data"]["data"][0]["event"]["name"] == "CREATE_ORDER"
    search, action_id, event_id, page = service.list.call_args.args
    assert (search, action_id, event_id, page.page, page.page_size) == ("aaaa", 1, 2, 2, 5)


def test_list_defaults(api_client, service, as_admin):
    service.list.return_value = AdminPage(data=[], total_count=0)

    assert api_client.get(BASE).status_code == 200
    search, action_id, event_id, page = service.list.call_args.args
    assert (search, action_id, event_id, page.page, page.page_size) == (None, None, None, 1, 20)


@pytest.mark.parametrize("params", [{"page_size": 101}, {"action_id": "x"}, {"event_id": 0}, {"foo": "bar"},
                                    {"page": 0}])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)
    service.list.assert_not_called()


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)


def test_list_database_error_is_502(api_client, service, as_admin):
    service.list.side_effect = AdminUpstreamError("DATABASE_ERROR")
    _failed(api_client.get(BASE), 502)


def test_actions_and_events(api_client, service, as_admin):
    service.actions.return_value = [{"id": 1, "name": "DISCOUNT_AMOUNT"}]
    service.events.return_value = [{"id": 1, "name": "CREATE_ORDER"}, {"id": 2, "name": "CREATE_ACCOUNT"}]

    assert api_client.get(f"{BASE}/actions").json()["data"] == [{"id": 1, "name": "DISCOUNT_AMOUNT"}]
    assert [e["name"] for e in api_client.get(f"{BASE}/events").json()["data"]] == ["CREATE_ORDER", "CREATE_ACCOUNT"]


def test_actions_require_read_permission(api_client, service):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="3", email="x@example.com", role=ROLE_ADMIN, permissions=frozenset({"admin.users.read"}), token="t")
    try:
        _failed(api_client.get(f"{BASE}/actions"), 403)
        _failed(api_client.get(f"{BASE}/events"), 403)
    finally:
        esim_app.dependency_overrides.pop(admin_user, None)


# ---------- create ----------

def test_create_returns_201_with_audit_context(api_client, service, as_admin):
    service.create.return_value = ROW

    response = api_client.post(BASE, json=BODY, headers={"X-Forwarded-For": "9.9.9.9"})

    assert response.status_code == 201
    assert response.json()["responseCode"] == 201
    assert response.json()["data"]["name"] == "DISCOUNT_AMOUNT-CREATE_ORDER-3"
    body, ctx = service.create.call_args.args
    assert body.model_dump() == BODY
    assert ctx.actor_id == "11111111-1111-1111-1111-111111111111"
    assert ctx.ip == "9.9.9.9"


def test_create_duplicate_is_409_duplicate_rule(api_client, service, as_admin):
    service.create.side_effect = AdminConflict("duplicate_rule")

    response = api_client.post(BASE, json=BODY)

    _failed(response, 409)
    assert _code(response) == "duplicate_rule"


def test_create_unknown_action_is_422(api_client, service, as_admin):
    service.create.side_effect = AdminValidationError("promotion_rule_action_id: unknown action")
    _failed(api_client.post(BASE, json={**BODY, "promotion_rule_action_id": 99}), 422)


@pytest.mark.parametrize("payload", [
    {**BODY, "max_usage": 0},
    {**BODY, "beneficiary": 3},
    {**BODY, "beneficiary": -1},
    {**BODY, "promotion_rule_action_id": "x"},
    {**BODY, "name": "forced"},
    {"max_usage": 1},
])
def test_create_invalid_body_is_422(api_client, service, as_admin, payload):
    _failed(api_client.post(BASE, json=payload), 422)
    service.create.assert_not_called()


def test_create_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.post(BASE, json=BODY), 403)
    service.create.assert_not_called()


def test_create_without_session_is_401(api_client, service):
    _failed(api_client.post(BASE, json=BODY), 401)


# ---------- update ----------

def test_update_returns_200(api_client, service, as_admin):
    service.update.return_value = {**ROW, "beneficiary": 2}

    response = api_client.put(f"{BASE}/{RULE_ID}", json={**BODY, "beneficiary": 2})

    assert response.status_code == 200
    assert response.json()["data"]["beneficiary"] == 2
    rule_id, body, ctx = service.update.call_args.args
    assert rule_id == RULE_ID and body.beneficiary == 2 and ctx.actor_id


def test_update_duplicate_is_409(api_client, service, as_admin):
    service.update.side_effect = AdminConflict("duplicate_rule")
    response = api_client.put(f"{BASE}/{RULE_ID}", json=BODY)
    _failed(response, 409)
    assert _code(response) == "duplicate_rule"


def test_update_locked_field_is_422_rule_field_locked(api_client, service, as_admin):
    """TEOS-212: changing action, event or max_usage on edit is refused."""
    service.update.side_effect = AdminValidationError(
        json.dumps({"code": "rule_field_locked", "fields": ["max_usage"]}))
    response = api_client.put(f"{BASE}/{RULE_ID}", json={**BODY, "max_usage": 4})
    _failed(response, 422)
    assert _code(response) == "rule_field_locked"


def test_update_unknown_id_is_404(api_client, service, as_admin):
    service.update.side_effect = AdminNotFound("not_found")
    _failed(api_client.put(f"{BASE}/{RULE_ID}", json=BODY), 404)


def test_update_bad_id_or_body_is_422(api_client, service, as_admin):
    _failed(api_client.put(f"{BASE}/not-a-uuid", json=BODY), 422)
    _failed(api_client.put(f"{BASE}/{RULE_ID}", json={**BODY, "extra": 1}), 422)
    service.update.assert_not_called()


def test_update_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.put(f"{BASE}/{RULE_ID}", json=BODY), 403)


# ---------- delete ----------

def test_delete_returns_204_empty(api_client, service, as_admin):
    response = api_client.delete(f"{BASE}/{RULE_ID}")

    assert response.status_code == 204
    assert response.content == b""
    assert service.delete.call_args.args[0] == RULE_ID


def test_delete_rule_in_use_is_409(api_client, service, as_admin):
    service.delete.side_effect = AdminConflict("rule_in_use")

    response = api_client.delete(f"{BASE}/{RULE_ID}")

    _failed(response, 409)
    assert _code(response) == "rule_in_use"


def test_delete_twice_second_is_404(api_client, service, as_admin):
    service.delete.side_effect = [None, AdminNotFound("not_found")]

    assert api_client.delete(f"{BASE}/{RULE_ID}").status_code == 204
    _failed(api_client.delete(f"{BASE}/{RULE_ID}"), 404)


def test_delete_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.delete(f"{BASE}/{RULE_ID}"), 403)
    service.delete.assert_not_called()


def test_lookup_still_above_id_routes(api_client, service, as_admin, monkeypatch):
    """GET /promotion-rules/lookup must not be read as /{rule_id}."""
    fake = MagicMock()
    fake.promotion_rules.return_value = AdminPage(data=[], total_count=0)
    monkeypatch.setattr("app.services.admin.lookup_service.service", fake)

    assert api_client.get(f"{BASE}/lookup").status_code == 200
    fake.promotion_rules.assert_called_once()
