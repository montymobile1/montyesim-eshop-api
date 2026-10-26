"""HTTP contract of /api/v1/admin/promotions (MS-5050, MS-5052, MS-5054)."""
import json
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.dependencies.admin_security import AdminUser, admin_user
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError, AdminValidationError
from app.schemas.admin.promotions import AdminPromotionPage, AdminPromotionUsagePage
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin/promotions"
PROMO_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RULE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
BUNDLE_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

RULE = {"id": RULE_ID, "name": "DISCOUNT_AMOUNT-CREATE_ORDER-3", "max_usage": 3, "beneficiary": 1,
        "action": {"id": 1, "name": "DISCOUNT_AMOUNT"}, "event": {"id": 1, "name": "CREATE_ORDER"}}
ROW = {"id": PROMO_ID, "name": "Summer", "code": "SUMMER10", "type": "Promo", "amount": 10.0,
       "valid_from": "2026-10-01T00:00:00", "valid_to": "2026-12-31T23:59:59", "is_active": True,
       "times_used": 0, "bundle_codes": ["EU5", "US1"], "rule_id": RULE_ID, "rule": RULE,
       "status": "active", "created_at": "2026-10-20T10:00:00"}
FACETS = {"all": 1, "active": 1, "inactive": 0, "expired": 0, "scheduled": 0}
CREATE = {"name": "Summer", "code": "SUMMER10", "rule_id": RULE_ID, "amount": 10,
          "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z", "bundle_codes": ["EU5", "US1"]}
USAGE_ROW = {"id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "created_at": "2026-10-20T10:00:00",
             "promotion_code": "SUMMER10", "promotion": {"id": PROMO_ID, "code": "SUMMER10", "name": "Summer"},
             "user": {"id": USER_ID, "email": "u@example.com", "msisdn": "+1"}, "bundle_id": BUNDLE_ID,
             "bundle_code": "EU5", "order_id": None, "discount_amount": 2.5, "currency": "USD", "status": "completed"}
TOTALS = {"uses": 1, "distinct_users": 1, "discount_amount": [{"currency": "USD", "amount": 2.5}]}


@pytest.fixture
def service(monkeypatch):
    from app.api.v1.admin import promotions as routes
    fake = MagicMock(name="promotion-service")
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
    """An admin holding only admin.promotions.read."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="22222222-2222-2222-2222-222222222222", email="reader@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.promotions.read"}), token="t")
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


@pytest.fixture
def as_other():
    """An admin without any promotions permission."""
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: AdminUser(
        id="33333333-3333-3333-3333-333333333333", email="other@example.com", role=ROLE_ADMIN,
        permissions=frozenset({"admin.users.read"}), token="t")
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def _failed(response, status):
    body = response.json()
    assert response.status_code == status
    assert body["status"] == "failed"
    assert body["responseCode"] == status
    return body


def _details(response):
    return json.loads(response.json()["developerMessage"])


# ---------- list ----------

def test_list_returns_rows_facets_and_bundle_codes(api_client, service, as_admin):
    service.list.return_value = AdminPromotionPage(data=[ROW], total_count=1, facets=FACETS)

    response = api_client.get(BASE, params=[("search", "a,code.ilike.%"), ("status", "expired"),
                                            ("rule_ids", RULE_ID), ("bundle_codes", "EU5"), ("bundle_codes", "US)1"),
                                            ("include_global", "true"), ("page", "2"), ("page_size", "5")])

    assert response.status_code == 200
    body = response.json()
    assert body["totalCount"] == 1
    assert body["data"]["facets"] == FACETS
    assert body["data"]["data"][0]["bundle_codes"] == ["EU5", "US1"]
    assert body["data"]["data"][0]["rule"]["action"]["name"] == "DISCOUNT_AMOUNT"
    assert "callback_url" not in body["data"]["data"][0]
    page = service.list.call_args.args[0]
    kwargs = service.list.call_args.kwargs
    assert (page.page, page.page_size) == (2, 5)
    # user input reaches the service as plain values, never as a filter string
    assert kwargs == {"search": "a,code.ilike.%", "status": "expired", "rule_ids": [RULE_ID],
                      "bundle_codes": ["EU5", "US)1"], "include_global": True}


def test_list_defaults_to_all_page_one(api_client, service, as_admin):
    service.list.return_value = AdminPromotionPage(data=[], total_count=0,
                                                   facets={k: 0 for k in FACETS})

    assert api_client.get(BASE).status_code == 200
    page = service.list.call_args.args[0]
    assert (page.page, page.page_size) == (1, 20)
    assert service.list.call_args.kwargs == {"search": None, "status": "all", "rule_ids": None,
                                             "bundle_codes": None, "include_global": False}


@pytest.mark.parametrize("params", [{"page_size": 0}, {"status": "foo"}, {"rule_ids": "not-a-uuid"},
                                    {"foo": "bar"}, {"include_global": "maybe"}])
def test_list_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(BASE, params=params), 422)
    service.list.assert_not_called()


def test_list_without_session_is_401(api_client, service):
    _failed(api_client.get(BASE), 401)


def test_list_without_permission_is_403(api_client, service, as_other):
    _failed(api_client.get(BASE), 403)


def test_list_database_error_is_502(api_client, service, as_admin):
    service.list.side_effect = AdminUpstreamError("DATABASE_ERROR")
    _failed(api_client.get(BASE), 502)


# ---------- get ----------

def test_get_returns_promotion(api_client, service, as_admin):
    service.get.return_value = ROW
    response = api_client.get(f"{BASE}/{PROMO_ID}")
    assert response.status_code == 200
    assert response.json()["data"]["code"] == "SUMMER10"
    service.get.assert_called_once_with(PROMO_ID)


def test_get_unknown_is_404_and_bad_id_422(api_client, service, as_admin):
    service.get.side_effect = AdminNotFound("not_found")
    _failed(api_client.get(f"{BASE}/{PROMO_ID}"), 404)
    _failed(api_client.get(f"{BASE}/abc"), 422)


# ---------- usage ----------

def test_usage_returns_rows_and_totals(api_client, service, as_admin):
    service.usage.return_value = AdminPromotionUsagePage(data=[USAGE_ROW], total_count=1, totals=TOTALS)

    response = api_client.get(f"{BASE}/usage", params=[("code", "SUM,)%"), ("user_id", USER_ID),
                                                       ("bundle_ids", BUNDLE_ID), ("from", "2026-01-01"),
                                                       ("to", "2026-12-31"), ("page", "3")])

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["totals"] == TOTALS
    assert body["data"][0]["user"]["email"] == "u@example.com"
    assert body["data"][0]["promotion"]["name"] == "Summer"
    page = service.usage.call_args.args[0]
    assert page.page == 3
    kwargs = service.usage.call_args.kwargs
    assert kwargs["code"] == "SUM,)%"
    assert kwargs["user_id"] == USER_ID
    assert kwargs["bundle_ids"] == [BUNDLE_ID]
    assert (kwargs["date_from"].isoformat(), kwargs["date_to"].isoformat()) == ("2026-01-01", "2026-12-31")


def test_usage_is_not_read_as_a_promotion_id(api_client, service, as_admin):
    service.usage.return_value = AdminPromotionUsagePage(data=[], total_count=0,
                                                         totals={"uses": 0, "distinct_users": 0, "discount_amount": []})
    assert api_client.get(f"{BASE}/usage").status_code == 200
    service.get.assert_not_called()


@pytest.mark.parametrize("params", [{"user_id": "x"}, {"bundle_ids": "x"}, {"from": "2026-13-01"},
                                    {"page_size": 101}, {"unknown": 1}])
def test_usage_rejects_bad_query(api_client, service, as_admin, params):
    _failed(api_client.get(f"{BASE}/usage", params=params), 422)
    service.usage.assert_not_called()


def test_usage_range_errors_are_422(api_client, service, as_admin):
    service.usage.side_effect = AdminValidationError("from: must be on or before to")
    _failed(api_client.get(f"{BASE}/usage", params={"from": "2026-02-01", "to": "2026-01-01"}), 422)


def test_usage_without_permission_is_403(api_client, service, as_other):
    _failed(api_client.get(f"{BASE}/usage"), 403)


# ---------- create ----------

def test_create_returns_201(api_client, service, as_admin):
    service.create.return_value = ROW

    response = api_client.post(BASE, json=CREATE, headers={"X-Forwarded-For": "9.9.9.9"})

    assert response.status_code == 201
    assert response.json()["responseCode"] == 201
    body, ctx = service.create.call_args.args
    assert body.code == "SUMMER10" and str(body.rule_id) == RULE_ID and body.bundle_codes == ["EU5", "US1"]
    assert body.valid_from.tzinfo is not None
    assert ctx.ip == "9.9.9.9" and ctx.actor_id == "11111111-1111-1111-1111-111111111111"


def test_create_without_amount_and_without_bundles(api_client, service, as_admin):
    service.create.return_value = {**ROW, "amount": None, "bundle_codes": []}
    payload = {k: v for k, v in CREATE.items() if k not in ("amount", "bundle_codes")}

    assert api_client.post(BASE, json=payload).status_code == 201
    body = service.create.call_args.args[0]
    assert body.amount is None and body.bundle_codes == []


@pytest.mark.parametrize("code", ["duplicate_code", "duplicate_name"])
def test_create_duplicate_is_409(api_client, service, as_admin, code):
    service.create.side_effect = AdminConflict(code)
    response = api_client.post(BASE, json=CREATE)
    _failed(response, 409)
    assert _details(response)["code"] == code


def test_create_unknown_bundle_code_or_rule_is_422(api_client, service, as_admin):
    service.create.side_effect = AdminValidationError("bundle_codes: unknown bundle code XX")
    _failed(api_client.post(BASE, json=CREATE), 422)


@pytest.mark.parametrize("payload", [
    {**CREATE, "callback_url": "http://x"},
    {**CREATE, "callback_headers": "x"},
    {**CREATE, "times_used": 5},
    {**CREATE, "is_active": False},
    {**CREATE, "type": "Promo"},
    {**CREATE, "code": "has space"},
    {**CREATE, "code": "X"},
    {**CREATE, "code": "X" * 51},
    {**CREATE, "name": "A"},
    {**CREATE, "amount": 0},
    {**CREATE, "amount": -3},
    {**CREATE, "valid_to": "2026-09-01T00:00:00Z"},
    {**CREATE, "bundle_codes": ["A", "B", "C", "D", "E", "F"]},
    {**CREATE, "bundle_codes": ["EU5,US1"]},
    {**CREATE, "bundle_codes": [" "]},
    {**CREATE, "rule_id": "nope"},
    {k: v for k, v in CREATE.items() if k != "valid_from"},
])
def test_create_invalid_body_is_422(api_client, service, as_admin, payload):
    _failed(api_client.post(BASE, json=payload), 422)
    service.create.assert_not_called()


def test_create_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.post(BASE, json=CREATE), 403)
    service.create.assert_not_called()


def test_create_without_session_is_401(api_client, service):
    _failed(api_client.post(BASE, json=CREATE), 401)


# ---------- update ----------

def test_update_sends_editable_fields(api_client, service, as_admin):
    service.update.return_value = {**ROW, "name": "Winter"}

    response = api_client.put(f"{BASE}/{PROMO_ID}", json={"name": "Winter", "bundle_codes": ["EU5"],
                                                          "valid_to": "2027-01-31T00:00:00Z"})

    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Winter"
    promotion_id, body, ctx = service.update.call_args.args
    assert promotion_id == PROMO_ID and body.name == "Winter" and body.bundle_codes == ["EU5"]
    assert body.code is None and body.rule_id is None and body.amount is None


def test_update_without_bundle_codes_passes_none_not_empty(api_client, service, as_admin):
    """TEOS-226: a rename alone must not read as "remove every bundle"."""
    service.update.return_value = {**ROW, "name": "Winter"}

    response = api_client.put(f"{BASE}/{PROMO_ID}", json={"name": "Winter", "valid_to": "2027-01-31T00:00:00Z"})

    assert response.status_code == 200
    assert service.update.call_args.args[1].bundle_codes is None


def test_update_used_promotion_is_409_promotion_used(api_client, service, as_admin):
    service.update.side_effect = AdminConflict("promotion_used", times_used=4, fields=["code"])

    response = api_client.put(f"{BASE}/{PROMO_ID}", json={"name": "Winter", "bundle_codes": [], "code": "NEW1"})

    _failed(response, 409)
    assert _details(response) == {"code": "promotion_used", "times_used": 4, "fields": ["code"]}


def test_update_unknown_is_404(api_client, service, as_admin):
    service.update.side_effect = AdminNotFound("not_found")
    _failed(api_client.put(f"{BASE}/{PROMO_ID}", json={"name": "Winter", "bundle_codes": []}), 404)


@pytest.mark.parametrize("payload", [
    {"name": "Winter", "bundle_codes": [], "times_used": 0},
    {"name": "Winter", "bundle_codes": [], "is_active": True},
    {"name": "Winter", "bundle_codes": [], "callback_url": "x"},
    {"bundle_codes": []},
    {"name": "Winter", "bundle_codes": ["A", "B", "C", "D", "E", "F"]},
])
def test_update_invalid_body_is_422(api_client, service, as_admin, payload):
    _failed(api_client.put(f"{BASE}/{PROMO_ID}", json=payload), 422)
    service.update.assert_not_called()


def test_update_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.put(f"{BASE}/{PROMO_ID}", json={"name": "Winter", "bundle_codes": []}), 403)


# ---------- status ----------

def test_status_patch_passes_reason(api_client, service, as_admin):
    service.set_status.return_value = {**ROW, "is_active": False}

    response = api_client.patch(f"{BASE}/{PROMO_ID}/status", json={"is_active": False, "reason": "campaign over"})

    assert response.status_code == 200
    assert response.json()["data"]["is_active"] is False
    promotion_id, body, ctx = service.set_status.call_args.args
    assert promotion_id == PROMO_ID and body.is_active is False and ctx.reason == "campaign over"


def test_status_same_state_is_409_no_change(api_client, service, as_admin):
    service.set_status.side_effect = AdminConflict("no_change")
    response = api_client.patch(f"{BASE}/{PROMO_ID}/status", json={"is_active": True})
    _failed(response, 409)
    assert _details(response)["code"] == "no_change"


def test_status_unknown_id_is_404(api_client, service, as_admin):
    service.set_status.side_effect = AdminNotFound("not_found")
    _failed(api_client.patch(f"{BASE}/{PROMO_ID}/status", json={"is_active": True}), 404)


def test_status_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.patch(f"{BASE}/{PROMO_ID}/status", json={"is_active": True}), 403)
    service.set_status.assert_not_called()


# ---------- delete ----------

def test_delete_returns_204_empty(api_client, service, as_admin):
    response = api_client.delete(f"{BASE}/{PROMO_ID}")
    assert response.status_code == 204
    assert response.content == b""
    assert service.delete.call_args.args[0] == PROMO_ID


def test_delete_used_is_409_with_times_used(api_client, service, as_admin):
    service.delete.side_effect = AdminConflict("promotion_used", times_used=3)
    response = api_client.delete(f"{BASE}/{PROMO_ID}")
    _failed(response, 409)
    assert _details(response) == {"code": "promotion_used", "times_used": 3}


def test_delete_twice_second_is_404(api_client, service, as_admin):
    service.delete.side_effect = [None, AdminNotFound("not_found")]
    assert api_client.delete(f"{BASE}/{PROMO_ID}").status_code == 204
    _failed(api_client.delete(f"{BASE}/{PROMO_ID}"), 404)


def test_delete_without_write_permission_is_403(api_client, service, as_reader):
    _failed(api_client.delete(f"{BASE}/{PROMO_ID}"), 403)
    service.delete.assert_not_called()


def test_delete_without_session_is_401(api_client, service):
    _failed(api_client.delete(f"{BASE}/{PROMO_ID}"), 401)
