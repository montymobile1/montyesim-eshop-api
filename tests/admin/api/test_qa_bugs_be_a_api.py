"""QA bugs of 2026-10-26 (backend, batch A) over HTTP: TEOS-147, 148, 163, 166, 210, 211, 215."""
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_SUPER_ADMIN
from app.dependencies.admin_security import admin_user
from app.schemas.admin.common import AdminPage, LookupItem
from tests.admin.conftest import make_admin

BASE = "/api/v1/admin"
USER_ID = "33333333-3333-3333-3333-333333333333"
BUNDLE_ID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def as_super(api_client):
    from app.main import esim_app
    esim_app.dependency_overrides[admin_user] = lambda: make_admin(ROLE_SUPER_ADMIN)
    yield
    esim_app.dependency_overrides.pop(admin_user, None)


def _service(monkeypatch, module):
    fake = MagicMock(name=f"{module}-service")
    monkeypatch.setattr(f"app.api.v1.admin.{module}.service", fake)
    return fake


def _422(response, text):
    body = response.json()
    assert response.status_code == 422 and body["responseCode"] == 422, body
    assert text in body["developerMessage"], body


# ---------- TEOS-147: users lookup refuses a 1-character search ----------

@pytest.fixture
def lookups(monkeypatch):
    fake = MagicMock(name="lookup-service")
    page = AdminPage[LookupItem](data=[LookupItem(id="u1", label="a@x.com")], total_count=1)
    fake.users.return_value = fake.groups.return_value = page
    monkeypatch.setattr("app.services.admin.lookup_service.service", fake)
    return fake


@pytest.mark.parametrize("search", ["a", " a ", "é"])
def test_users_lookup_with_one_character_is_422(api_client, as_super, lookups, search):
    _422(api_client.get(f"{BASE}/users/lookup", params={"search": search}),
         "search: String should have at least 2 characters")
    lookups.users.assert_not_called()


@pytest.mark.parametrize("search", [None, "", "   ", "ab", " ab "])
def test_users_lookup_empty_or_two_characters_is_200(api_client, as_super, lookups, search):
    params = {} if search is None else {"search": search}
    assert api_client.get(f"{BASE}/users/lookup", params=params).status_code == 200


def test_other_lookups_and_the_users_list_keep_one_character_searches(api_client, as_super, lookups, monkeypatch):
    users = _service(monkeypatch, "users")
    users.list.return_value = AdminPage(data=[], total_count=0, facets={"all": 0, "banned": 0, "active": 0})

    assert api_client.get(f"{BASE}/groups/lookup", params={"search": "a"}).status_code == 200
    assert api_client.get(f"{BASE}/users", params={"search": "a"}).status_code == 200


# ---------- TEOS-148: ban / unban reason ----------

@pytest.mark.parametrize("path, body", [("ban", {"hours": 1}), ("unban", {})])
@pytest.mark.parametrize("reason", ["a", "ab", "  ab  "])
def test_ban_and_unban_reason_under_3_characters_is_422(api_client, as_super, monkeypatch, path, body, reason):
    users = _service(monkeypatch, "users")

    _422(api_client.post(f"{BASE}/users/{USER_ID}/{path}", json={**body, "reason": reason}),
         "reason: String should have at least 3 characters")
    users.ban.assert_not_called()
    users.unban.assert_not_called()


def test_ban_reason_is_trimmed(api_client, as_super, monkeypatch):
    users = _service(monkeypatch, "users")
    users.ban.return_value = {"user_id": USER_ID, "email": None, "banned_until": None, "hours_banned": 1}

    assert api_client.post(f"{BASE}/users/{USER_ID}/ban", json={"hours": 1, "reason": "  abc  "}).status_code == 200
    assert users.ban.call_args.args[1].reason == "abc"


# ---------- TEOS-166: bundle status reason ----------

@pytest.mark.parametrize("method, path, body", [
    ("patch", f"bundles/{BUNDLE_ID}/status", {"is_active": False}),
    ("post", "bundles/status/bulk", {"is_active": False}),
    ("post", "bundles/status/bulk", {"is_active": True}),
])
@pytest.mark.parametrize("reason", ["a", "ab", "  ab "])
def test_bundle_status_reason_under_3_characters_is_422(api_client, as_super, monkeypatch, method, path, body,
                                                        reason):
    bundles = _service(monkeypatch, "bundles")

    _422(getattr(api_client, method)(f"{BASE}/{path}", json={**body, "reason": reason}),
         "reason: String should have at least 3 characters")
    bundles.set_status.assert_not_called()
    bundles.set_all_status.assert_not_called()


def test_bundle_deactivation_with_3_characters_is_accepted(api_client, as_super, monkeypatch):
    bundles = _service(monkeypatch, "bundles")
    bundles.set_status.return_value = {"id": BUNDLE_ID, "is_active": False}

    response = api_client.patch(f"{BASE}/bundles/{BUNDLE_ID}/status", json={"is_active": False, "reason": " abc "})

    assert response.status_code == 200
    assert bundles.set_status.call_args.args[1].reason == "abc"


# ---------- TEOS-210 / TEOS-211: promotion rules ----------

RULE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RULE_BODY = {"promotion_rule_action_id": 1, "promotion_rule_event_id": 1, "max_usage": 3, "beneficiary": 1}


def test_promotion_rule_list_rows_carry_promotion_count(api_client, as_super, monkeypatch):
    rules = _service(monkeypatch, "promotion_rules")
    rules.list.return_value = AdminPage(data=[{"id": RULE_ID, "promotion_count": 28},
                                              {"id": "r2", "promotion_count": 0}], total_count=2)

    rows = api_client.get(f"{BASE}/promotion-rules").json()["data"]["data"]

    assert [row["promotion_count"] for row in rows] == [28, 0]


@pytest.mark.parametrize("method, path", [("post", "promotion-rules"), ("put", f"promotion-rules/{RULE_ID}")])
def test_promotion_rule_max_usage_as_a_string_is_422(api_client, as_super, monkeypatch, method, path):
    rules = _service(monkeypatch, "promotion_rules")

    _422(getattr(api_client, method)(f"{BASE}/{path}", json={**RULE_BODY, "max_usage": "5"}), "max_usage")
    rules.create.assert_not_called()
    rules.update.assert_not_called()


# ---------- TEOS-215: voucher amount ----------

@pytest.mark.parametrize("amount", ["5", "5.5"])
def test_voucher_amount_as_a_string_is_422(api_client, as_super, monkeypatch, amount):
    vouchers = _service(monkeypatch, "vouchers")

    _422(api_client.post(f"{BASE}/vouchers", json={"amount": amount}), "amount")
    vouchers.create.assert_not_called()


@pytest.mark.parametrize("amount", [5, 5.5])
def test_voucher_amount_as_a_number_reaches_the_service(api_client, as_super, monkeypatch, amount):
    vouchers = _service(monkeypatch, "vouchers")
    vouchers.create.return_value = {"id": 1, "code": "ABCD", "amount": amount}

    assert api_client.post(f"{BASE}/vouchers", json={"amount": amount}).status_code == 201
    assert vouchers.create.call_args.args[0].amount == amount


# ---------- TEOS-163: OpenAPI documents the shared errors ----------

@pytest.fixture
def spec(api_client):
    return api_client.get("/openapi.json").json()


def _admin_operations(spec):
    for path, item in spec["paths"].items():
        if path.startswith(f"{BASE}/"):
            for method, operation in item.items():
                yield path, method, operation


def test_every_admin_route_documents_the_shared_errors_with_the_response_schema(spec):
    operations = list(_admin_operations(spec))
    assert len(operations) > 50
    for path, method, operation in operations:
        expected = {"401", "422", "502"} if path.startswith(f"{BASE}/auth/") else {"401", "403", "422", "502"}
        missing = expected - set(operation["responses"])
        assert not missing, (method, path, missing)
        for code in expected:
            schema = operation["responses"][code]["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/Response"}, (method, path, code)


def test_contact_us_documents_405(spec):
    responses = spec["paths"][f"{BASE}/contact-us"]["get"]["responses"]
    assert {"200", "401", "403", "405", "422", "502"} <= set(responses)


def test_legacy_routes_do_not_gain_the_admin_errors(spec):
    legacy = [op for path, item in spec["paths"].items() if not path.startswith(f"{BASE}/")
              for op in item.values()]
    assert legacy and not any("502" in op.get("responses", {}) for op in legacy)
