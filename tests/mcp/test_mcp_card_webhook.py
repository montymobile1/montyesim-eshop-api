"""MCP card webhook: signature, dedupe, ordering, verification, exactly-once provisioning.

Every event here is a fake payload handed to the *verified-webhook* entry point, or a
signed/unsigned HTTP request against the real callback route with ``stripe.Webhook``
patched. No Stripe call and no eSIM Hub call leaves the process.
"""

import asyncio
import json

import pytest
import stripe

from app.config.mcp_card_constants import McpCardStatus
from tests.mcp.conftest import (
    USER_A_TOKEN,
    USER_B_TOKEN,
    legacy_payment_intent,
    payment_intent,
    get_card_status,
    post_card,
    stripe_event,
)

WEBHOOK_PATH = "/api/v1/callback/payment-webhook"


def handle(card_webhook, event) -> dict:
    """Drive the async webhook handler from a sync test."""
    return asyncio.run(card_webhook.handle_event(event))



def data(response) -> dict:
    return response.json()["data"]


def checkouts(db) -> list:
    return db.tables.get("mcp_card_checkout", [])


def record(db, reference: str) -> dict:
    return next(row for row in checkouts(db) if row["id"] == reference)


def profiles(db) -> list:
    return db.tables.get("user_profile", [])


def orders(db) -> list:
    return db.tables.get("user_order", [])


def events(db) -> list:
    return db.tables.get("mcp_stripe_webhook_event", [])


@pytest.fixture
def paid_checkout(db, client, mcp_card_enabled, stripe_gateway, card_hub):
    """A created checkout awaiting payment."""
    response = post_card(client)
    assert response.status_code == 200
    return data(response)["payment_reference"]


# ------------------------------------------------------------ signature handling

def test_invalid_signature_is_rejected_and_changes_nothing(db, client, mcp_card_enabled,
                                                            stripe_gateway, card_hub, paid_checkout,
                                                            monkeypatch):
    def raise_signature_error(*_args, **_kwargs):
        raise stripe.error.SignatureVerificationError("bad signature", "sig")

    monkeypatch.setattr(stripe.Webhook, "construct_event", raise_signature_error)
    session = payment_intent(db, paid_checkout)

    response = client.post(WEBHOOK_PATH, json=stripe_event("payment_intent.succeeded", session),
                           headers={"stripe-signature": "t=1,v1=forged"})

    assert response.status_code == 400
    assert record(db, paid_checkout)["status"] == McpCardStatus.PENDING
    assert profiles(db) == []
    assert events(db) == []


def test_malformed_payload_is_rejected(db, client, mcp_card_enabled, stripe_gateway, card_hub,
                                        paid_checkout, monkeypatch):
    def raise_value_error(*_args, **_kwargs):
        raise ValueError("bad payload")

    monkeypatch.setattr(stripe.Webhook, "construct_event", raise_value_error)
    response = client.post(WEBHOOK_PATH, content=b"{not json",
                           headers={"stripe-signature": "t=1,v1=x"})

    assert response.status_code == 400
    assert profiles(db) == []


def test_a_verified_signature_is_processed(db, client, mcp_card_enabled, stripe_gateway, card_hub,
                                            paid_checkout, monkeypatch):
    session = payment_intent(db, paid_checkout)
    event = stripe_event("payment_intent.succeeded", session)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)

    response = client.post(WEBHOOK_PATH, json=event, headers={"stripe-signature": "t=1,v1=ok"})

    assert response.status_code == 200
    assert record(db, paid_checkout)["status"] == McpCardStatus.COMPLETED


# --------------------------------------------------------- redirect never pays

def test_a_browser_redirect_never_provisions(db, client, mcp_card_enabled, stripe_gateway, card_hub,
                                              paid_checkout):
    """There is no endpoint that a redirect could hit to mark this paid."""
    status = get_card_status(client, paid_checkout)

    assert data(status)["status"] == McpCardStatus.PENDING
    assert data(status)["provisioned"] is False
    assert profiles(db) == []
    assert record(db, paid_checkout).get("paid_at") is None
    # The success_url is a static configured page; it carries no token that could be
    # replayed against the API to claim payment.
    assert record(db, paid_checkout)["status"] == McpCardStatus.PENDING


# ------------------------------------------------------------- successful payment

def test_successful_payment_provisions_exactly_once(db, card_webhook, mcp_card_enabled,
                                                     stripe_gateway, card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout)
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "provisioned"
    assert record(db, paid_checkout)["status"] == McpCardStatus.COMPLETED
    assert record(db, paid_checkout)["provisioned_at"] is not None
    assert len(profiles(db)) == 1
    assert card_hub.create_order_calls == 1
    assert orders(db)[0]["payment_status"] == "success"


def test_async_payment_succeeded_also_provisions(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                                  card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout)
    result = handle(card_webhook, 
        stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "provisioned"
    assert len(profiles(db)) == 1


def test_a_completed_but_unpaid_session_does_not_provision(db, card_webhook, mcp_card_enabled,
                                                            stripe_gateway, card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout, status="canceled")
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "awaiting_payment"
    assert record(db, paid_checkout)["status"] == McpCardStatus.PENDING
    assert profiles(db) == []
    assert card_hub.create_order_calls == 0


# ------------------------------------------------------------ duplicate delivery

def test_duplicate_events_are_suppressed(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                          card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout)
    event = stripe_event("payment_intent.succeeded", session, event_id="evt_dup_1")

    first = handle(card_webhook, event)
    second = handle(card_webhook, event)
    third = handle(card_webhook, event)

    assert first["result"] == "provisioned"
    assert second["result"] == "duplicate"
    assert third["result"] == "duplicate"
    assert len(profiles(db)) == 1
    assert card_hub.create_order_calls == 1
    assert len(events(db)) == 1


def test_two_distinct_events_for_one_session_still_provision_once(db, card_webhook, mcp_card_enabled,
                                                                   stripe_gateway, card_hub,
                                                                   paid_checkout):
    """Different event ids bypass the ledger; the status CAS is the second gate."""
    session = payment_intent(db, paid_checkout)
    first = handle(card_webhook, 
        stripe_event("payment_intent.succeeded", session, event_id="evt_a"))
    second = handle(card_webhook, 
        stripe_event("payment_intent.succeeded", session, event_id="evt_b"))

    assert first["result"] == "provisioned"
    assert second["result"] == "already_terminal"
    assert len(profiles(db)) == 1
    assert card_hub.create_order_calls == 1


def test_concurrent_deliveries_provision_once(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                               card_hub, paid_checkout):
    from concurrent.futures import ThreadPoolExecutor

    session = payment_intent(db, paid_checkout)
    events_in = [stripe_event("payment_intent.succeeded", session, event_id=f"evt_{i}")
                 for i in range(4)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in [pool.submit(handle, card_webhook, e) for e in events_in]]

    assert sum(1 for r in results if r["result"] == "provisioned") == 1
    assert len(profiles(db)) == 1
    assert card_hub.create_order_calls == 1


# --------------------------------------------------------------- out of order

def test_a_late_expired_event_never_demotes_a_completed_checkout(db, card_webhook, mcp_card_enabled,
                                                                  stripe_gateway, card_hub,
                                                                  paid_checkout):
    session = payment_intent(db, paid_checkout)
    handle(card_webhook, stripe_event("payment_intent.succeeded", session, event_id="evt_1"))
    assert record(db, paid_checkout)["status"] == McpCardStatus.COMPLETED

    late = handle(card_webhook, 
        stripe_event("payment_intent.canceled", session, event_id="evt_2"))

    assert late["result"] == "ignored_terminal"
    assert record(db, paid_checkout)["status"] == McpCardStatus.COMPLETED
    assert len(profiles(db)) == 1


def test_a_late_async_failure_never_demotes_a_completed_checkout(db, card_webhook, mcp_card_enabled,
                                                                  stripe_gateway, card_hub,
                                                                  paid_checkout):
    session = payment_intent(db, paid_checkout)
    handle(card_webhook, stripe_event("payment_intent.succeeded", session, event_id="e1"))
    late = handle(card_webhook, 
        stripe_event("payment_intent.payment_failed", session, event_id="e2"))

    assert late["result"] == "ignored_terminal"
    assert record(db, paid_checkout)["status"] == McpCardStatus.COMPLETED


def test_a_success_after_an_expiry_does_not_provision(db, card_webhook, mcp_card_enabled,
                                                       stripe_gateway, card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout)
    handle(card_webhook, stripe_event("payment_intent.canceled", session, event_id="e1"))
    assert record(db, paid_checkout)["status"] == McpCardStatus.EXPIRED

    late = handle(card_webhook, stripe_event("payment_intent.succeeded", session,
                                                  event_id="e2"))

    assert late["result"] == "already_terminal"
    assert profiles(db) == []
    assert card_hub.create_order_calls == 0


# ------------------------------------------------------------------ verification

@pytest.mark.parametrize("overrides,reason", [
    ({"amount": 999}, "AMOUNT_MISMATCH"),
    ({"amount": 100000}, "AMOUNT_MISMATCH"),
])
def test_a_mismatched_amount_is_never_fulfilled(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                                 card_hub, paid_checkout, overrides, reason):
    session = payment_intent(db, paid_checkout, **overrides)
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "verification_failed"
    assert record(db, paid_checkout)["status"] == McpCardStatus.AMBIGUOUS
    assert record(db, paid_checkout)["failure_code"] == reason
    assert profiles(db) == []
    assert card_hub.create_order_calls == 0


def test_a_mismatched_currency_is_never_fulfilled(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                                   card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout, currency="eur")
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "verification_failed"
    assert record(db, paid_checkout)["failure_code"] == "CURRENCY_MISMATCH"
    assert profiles(db) == []


@pytest.mark.parametrize("key,value,reason", [
    ("user_id", "22222222-2222-2222-2222-222222222222", "USER_MISMATCH"),
    ("order_id", "00000000-0000-0000-0000-000000000000", "ORDER_MISMATCH"),
    ("bundle_code", "some-other-bundle", "BUNDLE_MISMATCH"),
])
def test_mismatched_metadata_is_never_fulfilled(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                                 card_hub, paid_checkout, key, value, reason):
    session = payment_intent(db, paid_checkout, metadata_overrides={key: value})
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "verification_failed"
    assert record(db, paid_checkout)["failure_code"] == reason
    assert profiles(db) == []
    assert card_hub.create_order_calls == 0


def test_a_session_without_the_mcp_marker_is_ignored(db, card_webhook, mcp_card_enabled,
                                                      stripe_gateway, card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout, metadata_overrides={"mcp_source": "something_else"})
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "ignored_not_mcp"
    assert record(db, paid_checkout)["status"] == McpCardStatus.PENDING
    assert profiles(db) == []
    assert events(db) == []


def test_an_unknown_checkout_is_ignored(db, card_webhook, mcp_card_enabled, stripe_gateway, card_hub,
                                         paid_checkout):
    session = payment_intent(db, paid_checkout)
    session["id"] = "cs_test_does_not_exist"
    session["metadata"]["checkout_id"] = "00000000-0000-0000-0000-000000000000"
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "ignored_unknown_checkout"
    assert profiles(db) == []


# ------------------------------------------------------------- expiry / failure

def test_an_expired_session_marks_the_checkout_expired(db, card_webhook, mcp_card_enabled,
                                                        stripe_gateway, card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout, status="canceled")
    result = handle(card_webhook, stripe_event("payment_intent.canceled", session))

    assert result["result"] == "expired"
    assert record(db, paid_checkout)["status"] == McpCardStatus.EXPIRED
    assert orders(db)[0]["order_status"] == "canceled"
    assert profiles(db) == []


def test_an_async_payment_failure_marks_the_checkout_failed(db, card_webhook, mcp_card_enabled,
                                                             stripe_gateway, card_hub, paid_checkout):
    session = payment_intent(db, paid_checkout, status="canceled")
    result = handle(card_webhook, 
        stripe_event("payment_intent.payment_failed", session))

    assert result["result"] == "failed"
    assert record(db, paid_checkout)["status"] == McpCardStatus.FAILED
    assert orders(db)[0]["payment_status"] == "failure"
    assert profiles(db) == []
    # No refund is attempted: nothing was captured and no business rule requires one.
    assert db.tables.get("user_wallet_transaction") is None


# -------------------------------------------- paid but provisioning failed (424)

def test_payment_success_with_provisioning_failure_is_recoverable(db, card_webhook, mcp_card_enabled,
                                                                   stripe_gateway, card_hub,
                                                                   paid_checkout):
    card_hub.fail_provisioning = True
    session = payment_intent(db, paid_checkout)
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "ambiguous_provisioning_failed"
    row = record(db, paid_checkout)
    assert row["status"] == McpCardStatus.AMBIGUOUS
    assert row["failure_code"] == "PROVISIONING_FAILED"
    assert row["paid_at"] is not None          # the payment is recorded, not lost
    assert profiles(db) == []
    # No automatic refund and no automatic retry.
    assert db.tables.get("user_wallet_transaction") is None


def test_a_provisioning_failure_is_not_retried_by_a_redelivery(db, card_webhook, mcp_card_enabled,
                                                                stripe_gateway, card_hub,
                                                                paid_checkout):
    card_hub.fail_provisioning = True
    session = payment_intent(db, paid_checkout)
    handle(card_webhook, stripe_event("payment_intent.succeeded", session, event_id="e1"))

    card_hub.fail_provisioning = False
    again = handle(card_webhook, 
        stripe_event("payment_intent.succeeded", session, event_id="e2"))

    # AMBIGUOUS is terminal for automation: a human reconciles it.
    assert again["result"] == "already_terminal"
    assert record(db, paid_checkout)["status"] == McpCardStatus.AMBIGUOUS
    assert card_hub.create_order_calls == 1


def test_a_provisioning_exception_is_contained(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                                card_hub, paid_checkout):
    card_hub.raise_on_provisioning = True
    session = payment_intent(db, paid_checkout)
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "ambiguous_provisioning_failed"
    assert record(db, paid_checkout)["status"] == McpCardStatus.AMBIGUOUS


# ----------------------------------------------------------- flag / isolation

def test_an_in_flight_payment_is_still_claimed_with_the_flag_off(db, card_webhook, monkeypatch,
                                                                 mcp_card_enabled, stripe_gateway,
                                                                 card_hub, paid_checkout):
    """The flag gates CREATING checkouts, not finishing ones already paid for.

    Turning it off must not strand a customer who has already been charged - and must
    not hand their intent to the legacy handler, which has no dedupe.
    """
    event = stripe_event("payment_intent.succeeded", payment_intent(db, paid_checkout))
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "false")

    assert card_webhook.handles(event) is True
    assert handle(card_webhook, event)["result"] == "provisioned"


def test_a_legacy_payment_intent_is_never_claimed(db, card_webhook, mcp_card_enabled):
    """Same event type as ours, but no mcp_source marker => it belongs to legacy."""
    for event_type in ("payment_intent.succeeded", "payment_intent.payment_failed",
                       "payment_intent.failed", "payment_intent.canceled"):
        assert card_webhook.handles(stripe_event(event_type, legacy_payment_intent())) is False


def test_unrelated_event_types_are_never_claimed(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                                  card_hub, paid_checkout):
    mcp_object = payment_intent(db, paid_checkout)
    for event_type in ("charge.refunded", "invoice.paid", "checkout.session.completed",
                       "customer.created"):
        assert card_webhook.handles(stripe_event(event_type, mcp_object)) is False


def test_an_mcp_payment_intent_is_claimed(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                           card_hub, paid_checkout):
    event = stripe_event("payment_intent.succeeded", payment_intent(db, paid_checkout))
    assert card_webhook.handles(event) is True


def test_the_intent_metadata_matches_the_legacy_payload_shape(db, stripe_gateway, mcp_card_enabled,
                                                               card_hub, paid_checkout):
    """The same keys the legacy flow puts on its PaymentIntent, plus our marker."""
    metadata = stripe_gateway.created[0]["metadata"]
    legacy_keys = {"order_id", "user_id", "device_id", "bundle_code", "order_type", "env",
                   "rule_id", "amount"}

    assert legacy_keys.issubset(set(metadata))
    assert metadata["mcp_source"] == "mcp_card_checkout_v1"
    assert metadata["order_type"] == "Assign"
    assert metadata["amount"] == "1000"


def test_no_raw_payload_or_secret_is_logged(db, card_webhook, mcp_card_enabled, stripe_gateway,
                                             card_hub, paid_checkout, caplog):
    session = payment_intent(db, paid_checkout)
    session["client_secret"] = "cs_test_0001_secret_supersecretvalue"
    with caplog.at_level("DEBUG"):
        handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "supersecretvalue" not in logged
    assert "_secret_" not in logged
    assert json.dumps(session) not in logged
