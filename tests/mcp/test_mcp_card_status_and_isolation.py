"""MCP card: status polling, ownership isolation, and legacy non-interference.

The isolation tests are the ones that matter most for this phase: they assert that
turning the card feature on changes nothing about the legacy Card flow, the legacy
webhook, the Wallet flow or the MCP Wallet flow.
"""

import asyncio
import json

import pytest

from app.config.mcp_card_constants import McpCardStatus
from tests.mcp.conftest import (
    ANONYMOUS_TOKEN,
    BUNDLE_CODE,
    USER_A_TOKEN,
    USER_B_TOKEN,
    VALID_KEY,
    card_body,
    legacy_payment_intent,
    payment_intent,
    get_card_status,
    mcp_headers,
    post_card,
    post_mcp,
    stripe_event,
)

STATUS_PATH = "/api/v1/mcp/user/bundle/card/status"
LEGACY_ASSIGN = "/api/v1/user/bundle/assign"


def data(response) -> dict:
    return response.json()["data"]


def handle(card_webhook, event) -> dict:
    return asyncio.run(card_webhook.handle_event(event))


@pytest.fixture
def reference(db, client, mcp_card_enabled, stripe_gateway, card_hub):
    return data(post_card(client))["payment_reference"]


# ------------------------------------------------------------------ status poll

def test_status_returns_pending_after_creation(db, client, mcp_card_enabled, stripe_gateway,
                                                card_hub, reference):
    response = get_card_status(client, reference)
    body = data(response)

    assert response.status_code == 200
    assert body["payment_reference"] == reference
    assert body["status"] == McpCardStatus.PENDING
    assert body["amount"] == "10.00"
    assert body["currency"] == "USD"
    assert body["bundle_code"] == BUNDLE_CODE
    assert body["provisioned"] is False
    assert body["next_action"] == "OPEN_CHECKOUT_URL"


def test_status_tracks_the_lifecycle(db, client, card_webhook, mcp_card_enabled, stripe_gateway,
                                      card_hub, reference):
    handle(card_webhook, stripe_event("payment_intent.succeeded",
                                      payment_intent(db, reference)))
    body = data(get_card_status(client, reference))

    assert body["status"] == McpCardStatus.COMPLETED
    assert body["provisioned"] is True
    assert body["next_action"] == "GET_ESIM_BY_ORDER"
    assert body["order_id"] is not None


def test_status_reports_expiry(db, client, card_webhook, mcp_card_enabled, stripe_gateway, card_hub,
                                reference):
    handle(card_webhook, stripe_event("payment_intent.canceled",
                                      payment_intent(db, reference, status="canceled")))
    body = data(get_card_status(client, reference))

    assert body["status"] == McpCardStatus.EXPIRED
    assert body["next_action"] == "START_NEW_CHECKOUT"
    assert "new Idempotency-Key" in body["message"]


def test_status_reports_manual_intervention(db, client, card_webhook, mcp_card_enabled,
                                             stripe_gateway, card_hub, reference):
    card_hub.fail_provisioning = True
    handle(card_webhook, stripe_event("payment_intent.succeeded",
                                      payment_intent(db, reference)))
    body = data(get_card_status(client, reference))

    assert body["status"] == McpCardStatus.AMBIGUOUS
    assert body["next_action"] == "CONTACT_SUPPORT"
    assert body["provisioned"] is False


# ------------------------------------------------------------ ownership isolation

def test_another_user_cannot_read_the_payment(db, client, mcp_card_enabled, stripe_gateway,
                                               card_hub, reference):
    response = get_card_status(client, reference, token=USER_B_TOKEN)
    assert response.status_code == 404


def test_a_foreign_reference_is_indistinguishable_from_a_missing_one(db, client, mcp_card_enabled,
                                                                      stripe_gateway, card_hub,
                                                                      reference):
    """Identical status AND identical body: no existence oracle."""
    foreign = get_card_status(client, reference, token=USER_B_TOKEN)
    missing = get_card_status(client, "11111111-2222-3333-4444-555555555555", token=USER_B_TOKEN)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["developerMessage"] == missing.json()["developerMessage"]
    assert foreign.json()["title"] == missing.json()["title"]
    assert foreign.json()["data"] == missing.json()["data"]


@pytest.mark.parametrize("bad_reference", [
    "../../etc/passwd", "abc def", "'; drop table mcp_card_checkout; --", "a" * 65, "%00",
])
def test_malformed_references_are_rejected_safely(db, client, mcp_card_enabled, stripe_gateway,
                                                   card_hub, bad_reference):
    """Rejected either by the path bound (400/422) or by the service (404).

    Both are safe: neither reaches a database lookup, so neither can reveal whether a
    reference exists. What matters is that none of them returns 200 or 500.
    """
    response = get_card_status(client, bad_reference)

    assert response.status_code in (400, 404, 422)
    assert response.json().get("data") is None


def test_status_requires_authentication(db, client, mcp_card_enabled, stripe_gateway, card_hub,
                                         reference):
    response = client.get(f"{STATUS_PATH}/{reference}", headers={"X-Device-Id": "device-1"})
    assert response.status_code in (401, 403)


def test_status_rejects_anonymous_sessions(db, client, mcp_card_enabled, stripe_gateway, card_hub,
                                            reference):
    response = get_card_status(client, reference, token=ANONYMOUS_TOKEN)
    assert response.status_code == 401


def test_status_is_disabled_with_the_flag_off(db, client, mcp_card_enabled, stripe_gateway,
                                               card_hub, reference, monkeypatch):
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "false")
    assert get_card_status(client, reference).status_code == 503


def test_status_leaks_no_stripe_identifiers(db, client, mcp_card_enabled, stripe_gateway, card_hub,
                                             reference):
    body = json.dumps(get_card_status(client, reference).json())

    for forbidden in ("cs_test", "pi_test", "client_secret", "sk_test", "whsec_", "checkout_url"):
        assert forbidden not in body


def test_a_user_cannot_drive_another_users_checkout_via_a_forged_event(db, card_webhook,
                                                                        mcp_card_enabled,
                                                                        stripe_gateway, card_hub,
                                                                        reference):
    """Even a well-formed event must match the stored record's owner."""
    session = payment_intent(db, reference, metadata_overrides={
        "user_id": "22222222-2222-2222-2222-222222222222"})
    result = handle(card_webhook, stripe_event("payment_intent.succeeded", session))

    assert result["result"] == "verification_failed"
    assert db.tables.get("user_profile") is None or db.tables["user_profile"] == []


# --------------------------------------------------- legacy is not interfered with

def legacy_body():
    return {"bundle_code": BUNDLE_CODE, "payment_type": "Wallet",
            "related_search": {"region": None,
                               "countries": [{"iso3_code": "FRA", "country_name": "France"}]},
            "promo_code": None, "affiliate_code": None}


@pytest.mark.parametrize("card_flag", ["false", "true"])
def test_legacy_wallet_assign_is_unaffected_by_the_card_flag(db, hub, client, monkeypatch, card_flag):
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", card_flag)
    response = client.post(LEGACY_ASSIGN, json=legacy_body(),
                           headers={"Authorization": f"Bearer {USER_A_TOKEN}",
                                    "X-Device-Id": "device-1"})

    assert response.status_code == 200
    assert response.json()["data"]["payment_status"] == "COMPLETED"
    assert len(db.tables["user_order"]) == 1
    assert db.tables.get("mcp_card_checkout") is None


@pytest.mark.parametrize("card_flag", ["false", "true"])
def test_mcp_wallet_purchase_is_unaffected_by_the_card_flag(db, hub, client, mcp_enabled,
                                                             monkeypatch, card_flag):
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", card_flag)
    response = post_mcp(client)

    assert response.status_code == 200
    assert data(response)["status"] == "COMPLETED"
    assert db.tables.get("mcp_card_checkout") is None


def test_legacy_payment_intent_webhook_path_is_untouched(db, hub, client, card_webhook,
                                                          mcp_card_enabled, monkeypatch):
    """A payment_intent event must still go to the legacy handler, not to MCP."""
    import stripe

    seen = {}

    def fake_add_task(task):
        seen["task"] = task
        return True

    from app.api.v1 import callback
    monkeypatch.setattr(callback.service._CallbackService__task_executor, "add_task", fake_add_task)

    event = {"id": "evt_pi_1", "type": "payment_intent.succeeded",
             "data": {"object": {"id": "pi_1", "metadata": {}}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)

    response = client.post("/api/v1/callback/payment-webhook", json=event,
                           headers={"stripe-signature": "t=1,v1=ok"})

    assert response.status_code == 200
    # Legacy behaviour preserved: the event was queued on the legacy task executor.
    assert "task" in seen
    assert db.tables.get("mcp_stripe_webhook_event") is None


def test_an_mcp_marked_intent_never_reaches_the_legacy_handler(db, hub, client, mcp_card_enabled,
                                                                stripe_gateway, card_hub, reference,
                                                                monkeypatch):
    """Routing is by marker, so an MCP intent is never queued on the legacy executor.

    This matters because the legacy handler has no duplicate-event ledger: if it ever
    picked one of these up, a redelivered event would provision a second eSIM.
    """
    import stripe

    from app.api.v1 import callback

    queued = {}
    monkeypatch.setattr(callback.service._CallbackService__task_executor, "add_task",
                        lambda task: queued.setdefault("task", task))

    event = stripe_event("payment_intent.succeeded", payment_intent(db, reference))
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)

    response = client.post("/api/v1/callback/payment-webhook", json=event,
                           headers={"stripe-signature": "t=1,v1=ok"})

    assert response.status_code == 200
    assert "task" not in queued                             # NOT the legacy path
    assert len(db.tables["mcp_stripe_webhook_event"]) == 1   # MCP ledger recorded it
    assert len(db.tables["user_profile"]) == 1               # provisioned exactly once


def test_an_mcp_marked_intent_is_still_completed_with_the_flag_off(db, hub, client, mcp_card_enabled,
                                                                    stripe_gateway, card_hub,
                                                                    reference, monkeypatch):
    """Disabling the flag stops NEW checkouts; it must not strand a paid customer."""
    import stripe

    from app.api.v1 import callback

    queued = {}
    monkeypatch.setattr(callback.service._CallbackService__task_executor, "add_task",
                        lambda task: queued.setdefault("task", task))

    event = stripe_event("payment_intent.succeeded", payment_intent(db, reference))
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "false")

    response = client.post("/api/v1/callback/payment-webhook", json=event,
                           headers={"stripe-signature": "t=1,v1=ok"})

    assert response.status_code == 200
    assert "task" not in queued
    assert len(db.tables["user_profile"]) == 1


def test_a_lapsed_checkout_is_reported_expired_without_any_webhook(db, client, mcp_card_enabled,
                                                                    stripe_gateway, card_hub):
    """Safety net: expiry is derived from expires_at when no cancel event arrives.

    This feature requires no new webhook subscription, so `payment_intent.canceled`
    may not be subscribed at all. A lapsed checkout must still stop looking payable.
    """
    stripe_gateway.expiry_minutes = -5          # the session already lapsed
    lapsed = data(post_card(client))["payment_reference"]

    body = data(get_card_status(client, lapsed))

    assert body["status"] == McpCardStatus.EXPIRED
    assert body["next_action"] == "START_NEW_CHECKOUT"
    row = next(r for r in db.tables["mcp_card_checkout"] if r["id"] == lapsed)
    assert row["status"] == McpCardStatus.EXPIRED
    assert row["failure_code"] == "SESSION_EXPIRED"


def test_lazy_expiry_never_demotes_a_completed_checkout(db, client, card_webhook, mcp_card_enabled,
                                                         stripe_gateway, card_hub):
    """Clock-based inference must never override a verified payment."""
    stripe_gateway.expiry_minutes = -5
    lapsed = data(post_card(client))["payment_reference"]
    handle(card_webhook, stripe_event("payment_intent.succeeded", payment_intent(db, lapsed)))

    body = data(get_card_status(client, lapsed))

    assert body["status"] == McpCardStatus.COMPLETED
    assert body["provisioned"] is True


def test_a_legacy_intent_still_goes_to_the_legacy_handler(db, hub, client, mcp_card_enabled,
                                                           monkeypatch):
    """No mcp_source marker => the pre-existing path, byte for byte."""
    import stripe

    from app.api.v1 import callback

    queued = {}
    monkeypatch.setattr(callback.service._CallbackService__task_executor, "add_task",
                        lambda task: queued.setdefault("task", task))

    event = stripe_event("payment_intent.succeeded", legacy_payment_intent())
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)

    response = client.post("/api/v1/callback/payment-webhook", json=event,
                           headers={"stripe-signature": "t=1,v1=ok"})

    assert response.status_code == 200
    assert "task" in queued                                   # the legacy path
    assert db.tables.get("mcp_stripe_webhook_event") is None   # MCP never saw it


def test_card_checkout_never_touches_the_wallet(db, client, mcp_card_enabled, stripe_gateway,
                                                 card_hub, card_webhook, reference):
    wallet_before = next(row["amount"] for row in db.tables["user_wallet"]
                         if row["user_id"] == "11111111-1111-1111-1111-111111111111")
    handle(card_webhook, stripe_event("payment_intent.succeeded",
                                      payment_intent(db, reference)))
    wallet_after = next(row["amount"] for row in db.tables["user_wallet"]
                        if row["user_id"] == "11111111-1111-1111-1111-111111111111")

    assert wallet_before == wallet_after
    assert db.tables.get("user_wallet_transaction") is None
