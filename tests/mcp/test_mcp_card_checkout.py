"""MCP card checkout: feature flag, validation, Stripe session creation, idempotency.

Everything external is faked: Stripe (in-process gateway), Supabase (in-process db),
the eSIM Hub (stub). No test here can reach a real payment provider or provision a real
eSIM.
"""

import json

import pytest

from app.services.mcp_stripe_gateway import StripeGatewayError
from tests.mcp.conftest import (
    ANONYMOUS_TOKEN,
    BUNDLE_CODE,
    OTHER_VALID_KEY,
    USER_A_ID,
    USER_A_TOKEN,
    USER_B_TOKEN,
    VALID_KEY,
    card_body,
    mcp_headers,
    post_card,
)

CARD_PATH = "/api/v1/mcp/user/bundle/card/checkout"
BUNDLE_PRICE_MINOR = 1000  # the stub bundle is 10.00 USD


def data(response) -> dict:
    return response.json()["data"]


def checkouts(db) -> list:
    return db.tables.get("mcp_card_checkout", [])


def orders(db) -> list:
    return db.tables.get("user_order", [])


# ------------------------------------------------------------------ feature flag

def test_route_is_registered_even_while_disabled(client):
    assert CARD_PATH in [route.path for route in client.app.routes]


def test_disabled_by_default(db, hub, client, monkeypatch):
    monkeypatch.delenv("MCP_CARD_PURCHASE_ENABLED", raising=False)
    response = post_card(client)

    assert response.status_code == 503
    assert response.json()["developerMessage"] == "MCP card purchase is not enabled in this environment"
    assert checkouts(db) == []
    assert orders(db) == []


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_falsey_flag_values_keep_it_disabled(db, hub, client, monkeypatch, value):
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", value)
    assert post_card(client).status_code == 503


def test_card_flag_is_independent_of_the_wallet_flag(db, hub, client, monkeypatch, mcp_card_enabled,
                                                      stripe_gateway, card_hub):
    # Wallet explicitly off; card must still work.
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")
    assert post_card(client).status_code == 200


def test_wallet_endpoint_unaffected_when_card_flag_is_on(db, hub, client, mcp_enabled, monkeypatch):
    from tests.mcp.conftest import post_mcp
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "true")
    assert post_mcp(client).status_code == 200


# ----------------------------------------------------------------- authentication

def test_unauthenticated_request_is_rejected(db, hub, client, mcp_card_enabled):
    response = client.post(CARD_PATH, json=card_body(), headers={"X-Device-Id": "device-1",
                                                                 "Idempotency-Key": VALID_KEY})
    assert response.status_code in (401, 403)
    assert checkouts(db) == []


def test_anonymous_session_is_rejected(db, hub, client, mcp_card_enabled):
    response = post_card(client, token=ANONYMOUS_TOKEN)
    assert response.status_code == 401
    assert checkouts(db) == []


def test_missing_device_id_is_rejected(db, hub, client, mcp_card_enabled):
    response = client.post(CARD_PATH, json=card_body(),
                           headers={"Authorization": f"Bearer {USER_A_TOKEN}",
                                    "Idempotency-Key": VALID_KEY})
    assert response.status_code in (400, 422)


def test_missing_idempotency_key_is_rejected(db, hub, client, mcp_card_enabled):
    response = post_card(client, idempotency_key=None)
    assert response.status_code == 400
    assert checkouts(db) == []


@pytest.mark.parametrize("key", ["short", "a" * 31, "a" * 129, "a" * 31 + "/"])
def test_invalid_idempotency_keys_are_rejected(db, hub, client, mcp_card_enabled, key):
    assert post_card(client, idempotency_key=key).status_code == 400
    assert checkouts(db) == []


# ------------------------------------------------------------------ body validation

@pytest.mark.parametrize("field,value", [
    ("amount", 1),
    ("price", 0.01),
    ("tax", 0),
    ("final_price", 5),
    ("currency", "EUR"),
    ("payment_type", "Card"),
    ("payment_method_id", "pm_card_visa"),
    ("card_number", "4242424242424242"),
    ("cvc", "123"),
    ("exp_month", 12),
    ("stripe_token", "tok_visa"),
    ("user_id", "22222222-2222-2222-2222-222222222222"),
    ("order_id", "order-1"),
    ("access_token", "token"),
    ("promo_code", "FREE"),
])
def test_payment_and_price_fields_are_rejected(db, hub, client, mcp_card_enabled, stripe_gateway,
                                                field, value):
    """The caller can never supply money or card data - extra='forbid' refuses it."""
    response = post_card(client, card_body(**{field: value}))

    assert response.status_code in (400, 422)
    assert checkouts(db) == []
    assert orders(db) == []
    assert stripe_gateway.created == []


def test_bundle_code_is_required(db, hub, client, mcp_card_enabled):
    body = card_body()
    body.pop("bundle_code")
    assert post_card(client, body).status_code in (400, 422)


def test_quote_reference_is_required(db, hub, client, mcp_card_enabled):
    body = card_body()
    body.pop("quote_reference")
    assert post_card(client, body).status_code in (400, 422)


@pytest.mark.parametrize("quote", ["<script>alert(1)</script>", "a" * 129, ""])
def test_invalid_quote_reference_is_rejected(db, hub, client, mcp_card_enabled, quote):
    assert post_card(client, card_body(quote_reference=quote)).status_code in (400, 422)
    assert checkouts(db) == []


def test_related_search_is_optional(db, hub, client, mcp_card_enabled, stripe_gateway, card_hub):
    body = card_body()
    body.pop("related_search")
    assert post_card(client, body).status_code == 200


# ---------------------------------------------------------------- bundle / pricing

def test_unknown_bundle_is_rejected_before_any_side_effect(db, hub, client, mcp_card_enabled,
                                                            stripe_gateway, card_hub):
    response = post_card(client, card_body(bundle_code="does-not-exist"))

    assert response.status_code == 400
    assert orders(db) == []
    assert stripe_gateway.created == []


def test_inactive_bundle_is_rejected(db, hub, client, mcp_card_enabled, stripe_gateway, card_hub):
    card_hub.bundle.is_active = False
    assert post_card(client).status_code == 400
    assert stripe_gateway.created == []


def test_non_applicable_stock_bundle_is_rejected(db, hub, client, mcp_card_enabled, stripe_gateway,
                                                  card_hub):
    card_hub.bundle.is_stockable = False
    card_hub.applicable = False
    assert post_card(client).status_code == 400
    assert stripe_gateway.created == []


def test_price_comes_from_the_hub_not_the_caller(db, hub, client, mcp_card_enabled, stripe_gateway,
                                                 card_hub):
    card_hub.bundle.original_price = 42.50
    response = post_card(client)

    assert response.status_code == 200
    assert stripe_gateway.created[0]["amount_minor"] == 4250
    assert data(response)["amount"] == "42.50"
    assert checkouts(db)[0]["amount_minor"] == 4250


@pytest.mark.parametrize("currency", ["EUR", "GBP", "eur"])
def test_a_non_system_currency_is_rejected(db, hub, client, mcp_card_enabled, stripe_gateway,
                                            card_hub, currency):
    response = client.post(CARD_PATH, json=card_body(),
                           headers={**mcp_headers(), "X-Currency": currency})

    assert response.status_code == 400
    assert checkouts(db) == []
    assert stripe_gateway.created == []


@pytest.mark.parametrize("currency", ["USD", "usd", " Usd "])
def test_the_system_currency_is_accepted_in_any_casing(db, hub, client, mcp_card_enabled,
                                                        stripe_gateway, card_hub, currency):
    response = client.post(CARD_PATH, json=card_body(),
                           headers={**mcp_headers(), "X-Currency": currency})
    assert response.status_code == 200


# ------------------------------------------------------- successful session creation

def test_successful_checkout_creates_exactly_one_session_and_one_order(db, hub, client,
                                                                        mcp_card_enabled,
                                                                        stripe_gateway, card_hub):
    response = post_card(client)

    assert response.status_code == 200
    assert len(stripe_gateway.created) == 1
    assert len(orders(db)) == 1
    assert len(checkouts(db)) == 1

    body = data(response)
    assert body["status"] == "PENDING"
    assert body["checkout_url"].startswith("https://checkout.stripe.com/")
    assert body["amount"] == "10.00"
    assert body["currency"] == "USD"
    assert body["order_id"] == orders(db)[0]["id"]
    assert body["payment_reference"] == checkouts(db)[0]["id"]
    assert body["expires_at"] is not None
    assert body["idempotent_replay"] is False
    assert response.headers["X-Idempotent-Replay"] == "false"


def test_the_session_is_created_with_card_only_payment_mode(db, hub, client, mcp_card_enabled,
                                                             stripe_gateway, card_hub):
    post_card(client)
    # The gateway pins mode/payment_method_types; assert the service never overrode them.
    from app.config.mcp_card_constants import STRIPE_MODE, STRIPE_PAYMENT_METHOD_TYPES
    assert STRIPE_MODE == "payment"
    assert STRIPE_PAYMENT_METHOD_TYPES == ["card"]
    assert stripe_gateway.created[0]["currency"] == "USD"


def test_the_order_is_pending_and_marked_as_a_card_order(db, hub, client, mcp_card_enabled,
                                                          stripe_gateway, card_hub):
    post_card(client)
    order = orders(db)[0]

    assert order["payment_type"] == "Card"
    assert order["order_type"] == "Assign"
    assert order["payment_status"] == "pending"
    assert order["order_status"] == "pending"
    assert order["amount"] == BUNDLE_PRICE_MINOR
    assert order["currency"] == "USD"
    assert order["user_id"] == USER_A_ID


def test_metadata_matches_the_legacy_payload_and_carries_no_secrets(db, hub, client,
                                                                     mcp_card_enabled,
                                                                     stripe_gateway, card_hub):
    """Same keys the legacy Card flow stamps, plus the routing marker and correlation.

    ``env`` IS present now, deliberately: the payload is the legacy one. What keeps
    these intents out of the legacy handler is the ``mcp_source`` marker, not the
    absence of ``env``.
    """
    post_card(client)
    metadata = stripe_gateway.created[0]["metadata"]

    legacy_keys = {"order_id", "user_id", "device_id", "bundle_code", "order_type", "env",
                   "rule_id", "amount"}
    assert legacy_keys.issubset(set(metadata))
    assert set(metadata) == legacy_keys | {"mcp_source", "checkout_id", "quote_reference"}

    assert metadata["mcp_source"] == "mcp_card_checkout_v1"
    assert metadata["bundle_code"] == BUNDLE_CODE
    assert metadata["user_id"] == USER_A_ID
    assert metadata["order_type"] == "Assign"
    assert metadata["amount"] == str(BUNDLE_PRICE_MINOR)
    assert metadata["device_id"] == "device-1"
    # promo_code is omitted, not null: MCP card checkout never applies a promotion and
    # Stripe rejects null metadata values.
    assert "promo_code" not in metadata

    rendered = json.dumps(metadata)
    for forbidden in (VALID_KEY, USER_A_TOKEN, "Bearer", "otp", "secret", "sk_test", "password"):
        assert forbidden not in rendered


def test_no_stripe_secret_is_returned_to_the_caller(db, hub, client, mcp_card_enabled,
                                                    stripe_gateway, card_hub):
    """The hosted URL is the deliverable; every Stripe *secret* must be absent.

    The checkout URL legitimately embeds the session id - that is how Stripe hosted
    checkout works and it is not a secret - so the assertion targets credentials.
    """
    payload = post_card(client).json()
    body = json.dumps(payload)

    for forbidden in ("client_secret", "sk_test", "sk_live", "rk_test", "whsec_", "_secret_",
                      "api_key"):
        assert forbidden not in body
    # The response model itself exposes no Stripe identifiers beyond the URL.
    assert "stripe_session_id" not in payload["data"]
    assert "stripe_payment_intent_id" not in payload["data"]


# ------------------------------------------------------------------- idempotency

def test_same_key_and_same_request_replays_the_same_checkout(db, hub, client, mcp_card_enabled,
                                                              stripe_gateway, card_hub):
    first = post_card(client)
    second = post_card(client)

    assert (first.status_code, second.status_code) == (200, 200)
    assert data(second)["idempotent_replay"] is True
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert data(second)["payment_reference"] == data(first)["payment_reference"]
    assert data(second)["checkout_url"] == data(first)["checkout_url"]
    assert len(stripe_gateway.created) == 1
    assert len(orders(db)) == 1
    assert len(checkouts(db)) == 1


def test_replay_is_stable_across_many_retries(db, hub, client, mcp_card_enabled, stripe_gateway,
                                               card_hub):
    reference = data(post_card(client))["payment_reference"]
    for _ in range(5):
        assert data(post_card(client))["payment_reference"] == reference
    assert len(stripe_gateway.created) == 1
    assert len(orders(db)) == 1


@pytest.mark.parametrize("changed", [
    {"bundle_code": "another-bundle"},
    {"quote_reference": "mcp-quote-card-2"},
    {"related_search": {"region": None, "countries": [{"iso3_code": "ITA", "country_name": "Italy"}]}},
])
def test_same_key_with_a_changed_request_is_a_conflict(db, hub, client, mcp_card_enabled,
                                                        stripe_gateway, card_hub, changed):
    post_card(client)
    conflict = post_card(client, card_body(**changed))

    assert conflict.status_code == 409
    assert "already used for a different checkout" in conflict.json()["developerMessage"]
    assert len(stripe_gateway.created) == 1
    assert len(orders(db)) == 1


def test_a_new_key_starts_a_new_checkout(db, hub, client, mcp_card_enabled, stripe_gateway, card_hub):
    first = post_card(client)
    second = post_card(client, idempotency_key=OTHER_VALID_KEY)

    assert data(second)["payment_reference"] != data(first)["payment_reference"]
    assert len(stripe_gateway.created) == 2
    assert len(orders(db)) == 2


def test_the_stripe_idempotency_key_is_the_same_stable_identity(db, hub, client, mcp_card_enabled,
                                                                 stripe_gateway, card_hub):
    post_card(client)
    key = stripe_gateway.created[0]["idempotency_key"]

    assert key.startswith("mcp-card-")
    assert len(key) == len("mcp-card-") + 64      # domain prefix + sha256 digest
    assert VALID_KEY not in key                   # never the raw caller key


def test_a_wallet_key_and_a_card_key_do_not_collide(db, hub, client, mcp_enabled, mcp_card_enabled,
                                                     stripe_gateway, card_hub):
    """The same external key under two operations must be two independent identities."""
    from tests.mcp.conftest import post_mcp
    wallet = post_mcp(client)
    card = post_card(client)

    assert (wallet.status_code, card.status_code) == (200, 200)
    assert data(card)["idempotent_replay"] is False
    assert len(db.tables["mcp_purchase_idempotency"]) == 2
    assert len({row["operation"] for row in db.tables["mcp_purchase_idempotency"]}) == 2


def test_concurrent_requests_create_at_most_one_session(db, hub, mcp_card_enabled, stripe_gateway,
                                                         card_hub):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app.main import esim_app

    def call():
        return post_card(TestClient(esim_app, raise_server_exceptions=False))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(call), pool.submit(call)]]

    statuses = sorted(r.status_code for r in responses)
    assert statuses in ([200, 200], [200, 409])
    assert len(stripe_gateway.created) == 1
    assert len(orders(db)) == 1
    assert len(checkouts(db)) == 1


# ------------------------------------------------------- stripe failure handling

def test_an_ambiguous_stripe_timeout_does_not_create_a_second_session(db, hub, client,
                                                                       mcp_card_enabled,
                                                                       stripe_gateway, card_hub):
    stripe_gateway.fail_with = StripeGatewayError("UNKNOWN", "timeout", retryable=True,
                                                  ambiguous=True)
    first = post_card(client)

    assert first.status_code == 503
    assert data(first)["status"] == "AMBIGUOUS"
    assert "same Idempotency-Key" in data(first)["message"]
    assert checkouts(db)[0]["status"] == "AMBIGUOUS"

    # Retrying the SAME key reuses the same Stripe idempotency key; the fake gateway
    # (like Stripe) returns the original session rather than making a new one.
    second = post_card(client)
    assert second.status_code == 200
    assert len(stripe_gateway.created) == 1
    assert len(orders(db)) == 1


def test_a_terminal_stripe_error_fails_the_checkout_safely(db, hub, client, mcp_card_enabled,
                                                            stripe_gateway, card_hub):
    stripe_gateway.fail_with = StripeGatewayError("STRIPE_ERROR", "bad request")
    response = post_card(client)

    assert response.status_code == 502
    assert data(response)["status"] == "FAILED"
    assert data(response)["checkout_url"] is None
    assert checkouts(db)[0]["status"] == "FAILED"


def test_a_stripe_error_never_leaks_provider_detail(db, hub, client, mcp_card_enabled,
                                                     stripe_gateway, card_hub):
    stripe_gateway.fail_with = StripeGatewayError(
        "STRIPE_ERROR", "No such customer sk_test_51abcdef; request-id req_123")
    body = json.dumps(post_card(client).json())

    assert "sk_test_51abcdef" not in body
    assert "req_123" not in body
    assert "No such customer" not in body


# --------------------------------------------------------------- configuration

@pytest.mark.parametrize("env,value", [
    ("MCP_CARD_SUCCESS_URL", ""),
    ("MCP_CARD_CANCEL_URL", ""),
    ("MCP_CARD_SUCCESS_URL", "not-a-url"),
    ("MCP_CARD_CANCEL_URL", "ftp://example.test/x"),
    ("MCP_CARD_SUCCESS_URL", "http://production.example.test/ok"),
])
def test_invalid_redirect_configuration_fails_closed(db, hub, client, mcp_card_enabled,
                                                      stripe_gateway, card_hub, monkeypatch,
                                                      env, value):
    monkeypatch.setenv(env, value)
    response = post_card(client)

    assert response.status_code == 503
    assert stripe_gateway.created == []


def test_localhost_http_redirects_are_allowed_for_local_runs(db, hub, client, mcp_card_enabled,
                                                              stripe_gateway, card_hub, monkeypatch):
    monkeypatch.setenv("MCP_CARD_SUCCESS_URL", "http://localhost:3000/ok")
    monkeypatch.setenv("MCP_CARD_CANCEL_URL", "http://127.0.0.1:3000/cancel")
    assert post_card(client).status_code == 200


def test_missing_hash_secret_fails_closed(db, hub, client, mcp_card_enabled, stripe_gateway,
                                           card_hub, monkeypatch):
    monkeypatch.delenv("MCP_IDEMPOTENCY_HASH_SECRET", raising=False)
    response = post_card(client)

    assert response.status_code == 503
    assert checkouts(db) == []
    assert stripe_gateway.created == []
