"""Feature-flag behaviour and request validation for the MCP purchase endpoint."""

import pytest

from tests.mcp.conftest import (
    ANONYMOUS_TOKEN,
    BUNDLE_CODE,
    USER_A_TOKEN,
    VALID_KEY,
    mcp_body,
    mcp_headers,
    post_mcp,
)

MCP_PATH = "/api/v1/mcp/user/bundle/assign"


# ------------------------------------------------------------------ feature flag

def test_route_is_registered_even_while_disabled(client):
    assert MCP_PATH in [route.path for route in client.app.routes]


def test_disabled_by_default(db, hub, client, monkeypatch):
    monkeypatch.delenv("MCP_PURCHASE_ENABLED", raising=False)
    response = post_mcp(client)
    assert response.status_code == 503
    assert response.json()["responseCode"] == 503
    assert db.tables.get("user_order") is None
    assert db.tables.get("mcp_purchase_idempotency") is None


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_falsey_flag_values_keep_the_endpoint_disabled(db, hub, client, monkeypatch, value):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", value)
    assert post_mcp(client).status_code == 503


def test_enabled_flag_executes_the_purchase(db, hub, client, mcp_enabled):
    response = post_mcp(client)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "COMPLETED"


def test_disabled_response_does_not_leak_internals(db, hub, client, monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")
    body = post_mcp(client).json()
    assert body["data"] is None
    assert "MCP_PURCHASE_DISABLED" in str(body["developerMessage"]) or body["title"]


# -------------------------------------------------------------- header validation

def test_missing_idempotency_key_is_rejected(db, hub, client, mcp_enabled):
    response = post_mcp(client, idempotency_key=None)
    assert response.status_code == 400
    assert response.json()["developerMessage"].startswith("Idempotency-Key header is required")
    assert db.tables.get("user_order") is None


@pytest.mark.parametrize("key", ["   ", "short", "a" * 31, "a" * 129, "a" * 31 + "/", "a" * 31 + " x"])
def test_invalid_idempotency_keys_are_rejected(db, hub, client, mcp_enabled, key):
    response = post_mcp(client, idempotency_key=key)
    assert response.status_code == 400
    assert db.tables.get("user_order") is None
    assert db.tables.get("mcp_purchase_idempotency") is None


def test_missing_device_id_is_rejected(db, hub, client, mcp_enabled):
    response = client.post(MCP_PATH, json=mcp_body(),
                           headers={"Authorization": f"Bearer {USER_A_TOKEN}",
                                    "Idempotency-Key": VALID_KEY})
    assert response.status_code in (400, 422)
    assert db.tables.get("user_order") is None


def test_missing_bearer_token_is_rejected(db, hub, client, mcp_enabled):
    response = client.post(MCP_PATH, json=mcp_body(),
                           headers={"X-Device-Id": "device-1", "Idempotency-Key": VALID_KEY})
    assert response.status_code in (401, 403)
    assert db.tables.get("user_order") is None


def test_anonymous_session_is_rejected(db, hub, client, mcp_enabled):
    response = post_mcp(client, token=ANONYMOUS_TOKEN)
    assert response.status_code == 401
    assert db.tables.get("user_order") is None


# ---------------------------------------------------------------- body validation

@pytest.mark.parametrize("payment_type", ["Card", "DCB", "wallet", "WALLET", ""])
def test_only_wallet_payment_type_is_accepted(db, hub, client, mcp_enabled, payment_type):
    response = post_mcp(client, mcp_body(payment_type=payment_type))
    assert response.status_code == 400
    assert db.tables.get("user_order") is None
    assert db.tables.get("user_wallet_transaction") is None


def test_wallet_payment_type_is_accepted(db, hub, client, mcp_enabled):
    assert post_mcp(client, mcp_body(payment_type="Wallet")).status_code == 200


@pytest.mark.parametrize("field,value", [
    ("user_id", "22222222-2222-2222-2222-222222222222"),
    ("price", 0.01),
    ("amount", 1),
    ("tax", 0),
    ("wallet_balance", 9999),
    ("email", "attacker@example.test"),
    ("order_id", "order-1"),
    ("payment_status", "COMPLETED"),
    ("order_status", "SUCCESS"),
    ("access_token", "token"),
    ("promo_code", "FREE"),
])
def test_untrusted_body_fields_are_rejected(db, hub, client, mcp_enabled, field, value):
    response = post_mcp(client, mcp_body(**{field: value}))
    assert response.status_code == 400
    assert db.tables.get("user_order") is None
    assert db.tables.get("user_wallet_transaction") is None


def test_bundle_code_is_required(db, hub, client, mcp_enabled):
    body = mcp_body()
    body.pop("bundle_code")
    assert post_mcp(client, body).status_code == 400


def test_related_search_is_optional(db, hub, client, mcp_enabled):
    body = mcp_body()
    body.pop("related_search")
    response = post_mcp(client, body)
    assert response.status_code == 200
    order = db.tables["user_order"][0]
    assert order["searched_countries"] == '{"region":null,"countries":null}'


def test_quote_reference_is_optional_and_echoed_back(db, hub, client, mcp_enabled):
    response = post_mcp(client, mcp_body(quote_reference="quote-abc-123"))
    assert response.json()["data"]["quote_reference"] == "quote-abc-123"


def test_quote_reference_rejects_unsafe_values(db, hub, client, mcp_enabled):
    assert post_mcp(client, mcp_body(quote_reference="<script>alert(1)</script>")).status_code == 400


def test_unknown_bundle_is_rejected_before_any_side_effect(db, hub, client, mcp_enabled):
    response = post_mcp(client, mcp_body(bundle_code="does-not-exist"))
    assert response.status_code == 400
    assert response.json()["data"]["status"] == "FAILED"
    assert response.json()["data"]["payment_status"] == "NOT_CHARGED"
    assert db.tables.get("user_order") is None
    assert db.tables.get("user_wallet_transaction") is None


def test_inactive_bundle_is_rejected(db, hub, client, mcp_enabled):
    hub.bundle.is_active = False
    response = post_mcp(client)
    assert response.status_code == 400
    assert db.tables.get("user_order") is None


def test_non_applicable_stock_bundle_is_rejected(db, hub, client, mcp_enabled):
    hub.bundle.is_stockable = False
    hub.applicable = False
    response = post_mcp(client)
    assert response.status_code == 400
    assert db.tables.get("user_order") is None


def test_successful_response_contract(db, hub, client, mcp_enabled):
    response = post_mcp(client)
    data = response.json()["data"]
    assert data == {
        "status": "COMPLETED",
        "order_id": db.tables["user_order"][0]["id"],
        "payment_method": "Wallet",
        "payment_status": "COMPLETED",
        "order_status": "SUCCESS",
        "idempotent_replay": False,
        "provisioning_status": "COMPLETED",
        "next_action": "GET_ESIM_BY_ORDER",
        "quote_reference": "mcp-quote-reference-1",
        "correlation_id": data["correlation_id"],
        "message": None,
    }
    assert response.headers["X-Idempotent-Replay"] == "false"
    assert set(response.json()) == {"status", "totalCount", "data", "title", "message",
                                    "developerMessage", "responseCode"}


def test_bundle_code_is_taken_from_the_body_not_a_header(db, hub, client, mcp_enabled):
    response = client.post(MCP_PATH, json=mcp_body(), headers={**mcp_headers(), "X-Bundle-Code": "other"})
    assert response.status_code == 200
    assert db.tables["user_order"][0]["bundle_id"] == BUNDLE_CODE
