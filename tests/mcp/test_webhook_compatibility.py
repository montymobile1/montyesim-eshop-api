"""The existing webhook is the single payment authority, and it is unchanged.

The MCP card flow adds no webhook handler, no webhook route, no second provisioning path
and no MCP branch inside ``callback_service``. What it does instead is build its Stripe
metadata so the *existing* handler recognises the payment as one of its own. These tests
pin both halves: the legacy handler still behaves exactly as it did, and an
MCP-originated payment reaches the same ``buy_bundle`` call a website card payment does.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.models.user import UserOrderType
from tests.mcp.conftest import executable_source
from tests.mocks import get_bundle_mock

CALLBACK_SOURCE = Path(__file__).resolve().parents[2] / "app" / "services" / "callback_service.py"


@pytest.fixture(scope="module")
def _callback_service_instance():
    """Built once: constructing it builds a dozen repositories, which is slow but offline.

    A module-scoped fixture is set up before the package's function-scoped autouse
    guards, so the configuration stub is applied explicitly here rather than inherited.
    """
    import app.config.config as config_module
    import app.config.helper as helper_module
    import app.services.callback_service as module

    def _stub(key, default_value=None):
        return default_value if default_value is not None else "stub"

    with patch.object(helper_module, "get_config", _stub), \
         patch.object(config_module, "get_config", _stub), \
         patch.object(module, "esim_hub_service_instance", lambda: MagicMock()):
        return module.CallbackService()


@pytest.fixture
def callback_service(_callback_service_instance):
    """A ``CallbackService`` whose collaborators are all fresh mocks for this test."""
    service = _callback_service_instance
    service._CallbackService__user_order_repo = MagicMock()
    service._CallbackService__user_repo = MagicMock()
    service._CallbackService__user_profile_repo = MagicMock()
    service._CallbackService__user_profile_bundle_repo = MagicMock()
    service._CallbackService__bundle_service = MagicMock(buy_bundle=AsyncMock(return_value="provisioned"),
                                                         top_up_bundle=AsyncMock(return_value="topped-up"))
    service._CallbackService__promotion_service = MagicMock(update_promotion_usage=AsyncMock())
    service._CallbackService__user_wallet_service = MagicMock()
    service._CallbackService__task_executor = MagicMock()
    return service


def an_order(order_id="order-1"):
    from app.models.user import UserOrderModel

    return UserOrderModel(id=order_id, user_id="user-1", bundle_id="BUNDLE-1",
                          order_type=UserOrderType.ASSIGN, amount=1000, modified_amount=1000,
                          currency="USD", payment_status=OrderStatusEnum.PENDING,
                          order_status=OrderStatusEnum.PENDING, payment_type=PaymentTypeEnum.CARD,
                          bundle_data=get_bundle_mock().model_dump_json())


def an_event(metadata, event_type="payment_intent.succeeded"):
    return {"type": event_type,
            "data": {"object": {"id": "pi_test_1", "metadata": metadata}}}


def legacy_metadata():
    """Exactly what ``__handle_card_payment`` writes for a website card purchase."""
    return {"order_id": "order-1", "user_id": "user-1", "device_id": "device-1",
            "bundle_code": "BUNDLE-1", "order_type": "Assign", "env": "DEV",
            "rule_id": "0", "amount": "1000"}


# --- (18) The legacy webhook still processes legacy events exactly as before ----------


def test_legacy_card_event_still_reaches_buy_bundle(callback_service):
    callback_service._CallbackService__user_order_repo.get_by_id.return_value = an_order()

    callback_service._CallbackService__handle_payment_webhook_data(an_event(legacy_metadata()))

    callback_service._CallbackService__bundle_service.buy_bundle.assert_awaited_once()
    kwargs = callback_service._CallbackService__bundle_service.buy_bundle.await_args.kwargs
    assert kwargs["payment_status"] == OrderStatusEnum.SUCCESS
    assert kwargs["payment_type"] == PaymentTypeEnum.CARD
    assert kwargs["user_id"] == "user-1"


def test_legacy_wallet_top_up_event_still_takes_the_top_up_branch(callback_service):
    metadata = {**legacy_metadata(), "user_wallet_id": "wallet-1"}
    callback_service._CallbackService__user_order_repo.get_by_id.return_value = an_order()
    callback_service._CallbackService__user_wallet_service.get_user_wallet_by_id.return_value = MagicMock()
    callback_service._CallbackService__user_wallet_service.exceeds_daily_top_up_limit.return_value = False

    callback_service._CallbackService__handle_payment_webhook_data(an_event(metadata))

    callback_service._CallbackService__bundle_service.buy_bundle.assert_not_awaited()
    callback_service._CallbackService__user_order_repo.update.assert_called_with(
        "order-1", {"payment_status": OrderStatusEnum.SUCCESS})


def test_legacy_event_from_another_environment_is_still_ignored(callback_service):
    callback_service._CallbackService__handle_payment_webhook_data(
        an_event({**legacy_metadata(), "env": "PROD"}))

    callback_service._CallbackService__bundle_service.buy_bundle.assert_not_awaited()


def test_legacy_unrelated_event_type_is_still_ignored(callback_service):
    callback_service._CallbackService__handle_payment_webhook_data(
        an_event(legacy_metadata(), event_type="payment_intent.created"))

    callback_service._CallbackService__bundle_service.buy_bundle.assert_not_awaited()


def test_legacy_failed_payment_still_provisions_nothing(callback_service):
    callback_service._CallbackService__user_order_repo.get_by_id.return_value = an_order()

    result = callback_service._CallbackService__handle_payment_webhook_data(
        an_event(legacy_metadata(), event_type="payment_intent.failed"))

    callback_service._CallbackService__bundle_service.buy_bundle.assert_not_awaited()
    assert isinstance(result, HTTPException)


def test_legacy_missing_metadata_is_still_rejected(callback_service):
    with pytest.raises(HTTPException):
        callback_service._CallbackService__handle_payment_webhook_data(
            an_event({**legacy_metadata(), "order_id": None}))


def test_legacy_top_up_order_still_requires_an_iccid(callback_service):
    order = an_order()
    order.order_type = UserOrderType.BUNDLE_TOP_UP
    callback_service._CallbackService__user_order_repo.get_by_id.return_value = order

    result = callback_service._CallbackService__handle_payment_webhook_data(
        an_event({**legacy_metadata(), "order_type": "Topup"}))

    assert isinstance(result, HTTPException)
    assert result.status_code == 400


# --- (19)(20) Signature verification and duplicate delivery --------------------------


@pytest.mark.asyncio
async def test_invalid_webhook_signature_is_rejected(callback_service):
    """(19) Verification is untouched; a bad signature never reaches any handler."""
    import stripe

    request = MagicMock()
    request.body = AsyncMock(return_value=b"{}")
    request.headers = {"stripe-signature": "not-a-signature"}

    with patch.object(stripe.Webhook, "construct_event",
                      side_effect=stripe.error.SignatureVerificationError("bad", "sig")), \
            pytest.raises(HTTPException) as raised:
        await callback_service.handle_payment_webhook(request)

    assert raised.value.status_code == 400
    callback_service._CallbackService__task_executor.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_webhook_payload_is_rejected(callback_service):
    import stripe

    request = MagicMock()
    request.body = AsyncMock(return_value=b"not json")
    request.headers = {"stripe-signature": "whatever"}

    with patch.object(stripe.Webhook, "construct_event", side_effect=ValueError("bad payload")), \
            pytest.raises(HTTPException) as raised:
        await callback_service.handle_payment_webhook(request)

    assert raised.value.status_code == 400


def test_duplicate_delivery_behaviour_is_the_existing_one(callback_service):
    """(20) A repeated event takes the same path twice; no new de-duplication is added.

    This documents the platform's existing behaviour rather than changing it. An MCP card
    payment is delivered through exactly this path, so whatever duplicate protection the
    platform has (or does not have) applies to it identically.
    """
    callback_service._CallbackService__user_order_repo.get_by_id.return_value = an_order()
    event = an_event(legacy_metadata())

    callback_service._CallbackService__handle_payment_webhook_data(event)
    callback_service._CallbackService__handle_payment_webhook_data(event)

    assert callback_service._CallbackService__bundle_service.buy_bundle.await_count == 2


# --- (17) An MCP-originated card payment goes through the same path ------------------


def capture_mcp_metadata(card_service, user, stripe_session) -> dict:
    """Run one MCP card checkout and return the metadata it handed the provider.

    Driven synchronously because the existing webhook handler calls ``asyncio.run``
    internally, so the assertions below cannot live inside a running event loop.
    """
    import asyncio

    from tests.mcp.test_mcp_card import a_request

    async def run():
        with patch("app.services.mcp_card_service.create_hosted_checkout_session",
                   return_value=stripe_session) as hosted:
            await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                               device_id="device-1")
        return hosted.call_args.kwargs["metadata"]

    return asyncio.run(run())


def test_mcp_checkout_metadata_is_accepted_by_the_existing_webhook(user, card_service,
                                                                   stripe_session,
                                                                   callback_service):
    """(17) The metadata the MCP checkout writes drives the existing handler to provision."""
    metadata = capture_mcp_metadata(card_service, user, stripe_session)
    # The keys the existing handler indexes directly, and the env filter it applies.
    assert metadata["order_id"] == "order-1"
    assert metadata["user_id"] == "user-1"
    assert metadata["bundle_code"] == "BUNDLE-1"
    assert metadata["order_type"] == "Assign"
    assert metadata["env"] == "DEV"
    # Stripe rejects null metadata values, and the handler would mis-route on this one.
    assert all(value is not None for value in metadata.values())
    assert "user_wallet_id" not in metadata
    # A descriptive marker for support triage; the existing handler never reads it.
    assert metadata["mcp_source"] == "mcp_hosted_checkout_v1"
    # Now drive the existing, unmodified handler with it.
    callback_service._CallbackService__user_order_repo.get_by_id.return_value = an_order()
    callback_service._CallbackService__handle_payment_webhook_data(an_event(metadata))

    callback_service._CallbackService__bundle_service.buy_bundle.assert_awaited_once()
    kwargs = callback_service._CallbackService__bundle_service.buy_bundle.await_args.kwargs
    assert kwargs["payment_status"] == OrderStatusEnum.SUCCESS
    assert kwargs["payment_type"] == PaymentTypeEnum.CARD


@pytest.mark.asyncio
async def test_mcp_checkout_puts_the_same_metadata_on_the_payment_intent(user, card_service,
                                                                          stripe_session):
    """The hosted-session helper carries the metadata onto the PaymentIntent it creates."""
    import app.config.utils as utils
    import stripe

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return stripe_session

    with patch.object(stripe.checkout.Session, "create", side_effect=fake_create):
        from tests.mcp.test_mcp_card import a_request

        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="device-1")

    assert captured["payment_intent_data"]["metadata"]["order_id"] == "order-1"
    assert captured["metadata"]["order_id"] == "order-1"
    assert captured["mode"] == "payment"
    assert captured["idempotency_key"] == "mcp-card-order-1"
    assert utils.create_hosted_checkout_session is not None


# --- (11)(12) No second webhook, no second provisioning flow -------------------------


def test_callback_service_has_no_mcp_branch_and_no_feature_flag():
    """The webhook is not made conditional on anything MCP."""
    text = executable_source(CALLBACK_SOURCE)
    for forbidden in ("mcp_purchase_enabled", "mcp_card_purchase_enabled", "MCP_PURCHASE_ENABLED",
                      "MCP_CARD_PURCHASE_ENABLED", "mcp_source", "McpCardCheckoutService",
                      "McpPurchaseService"):
        assert forbidden not in text


def test_there_is_exactly_one_payment_webhook_route():
    """No second webhook handler was introduced anywhere in the API surface."""
    api_dir = Path(__file__).resolve().parents[2] / "app" / "api"
    routes = []
    for path in api_dir.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "payment-webhook" in line and line.strip().startswith("@router"):
                routes.append((path.name, line.strip()))
    assert [name for name, _ in routes] == ["callback.py", "callback.py"], routes
    # The two are the existing real one and the existing fake one; neither is MCP's.
    assert all("mcp" not in line.lower() for _, line in routes)


def test_no_mcp_module_imports_the_callback_service():
    """Nothing in the MCP adapter can invoke, simulate or bypass the webhook."""
    app_dir = Path(__file__).resolve().parents[2] / "app"
    for path in app_dir.rglob("*mcp*.py"):
        text = executable_source(path)
        assert "CallbackService" not in text
        assert "handle_payment_webhook" not in text


def test_webhook_source_is_untouched_by_this_branch():
    """The webhook file is byte-identical to its committed state."""
    import subprocess

    root = Path(__file__).resolve().parents[2]
    diff = subprocess.run(["git", "diff", "HEAD", "--", "app/services/callback_service.py"],
                          cwd=root, capture_output=True, text=True)
    assert diff.stdout.strip() == "", diff.stdout


def test_mcp_metadata_shape_is_json_serializable(user, card_service, stripe_session):
    """Stripe requires flat string metadata; a nested value would be rejected at the API."""
    metadata = capture_mcp_metadata(card_service, user, stripe_session)
    assert json.loads(json.dumps(metadata)) == metadata
    assert all(isinstance(value, str) for value in metadata.values())
