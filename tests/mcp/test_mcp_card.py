"""The MCP card adapter opens a payment page and reads it. It never takes a payment.

The properties pinned here are the ones that keep a chat client from being able to claim
money moved: creating a page charges nothing and provisions nothing, the request cannot
carry card data or an outcome, the link is validated rather than trusted, and status is a
read of existing order columns that mutates nothing.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.config.constants import ErrorMessages
from app.config.db import OrderStatusEnum
from app.exceptions import CustomException
from app.schemas.mcp_card import McpCardCheckoutRequest
from tests.mcp.conftest import executable_source, make_order

CARD_SERVICE_SOURCE = Path(__file__).resolve().parents[2] / "app" / "services" / "mcp_card_service.py"


def a_request(**overrides):
    payload = {"bundle_code": "BUNDLE-1", "quote_reference": "quote-1"}
    payload.update(overrides)
    return McpCardCheckoutRequest(**payload)


# --- (16) MCP never accepts card data -----------------------------------------------


def test_card_request_structurally_cannot_carry_card_data():
    """There is no field for any of these, and ``extra=forbid`` refuses the whole request."""
    for smuggled in ({"card_number": "4242424242424242"}, {"cvv": "123"}, {"cvc": "123"},
                     {"expiry": "12/30"}, {"cardholder": "A Traveller"}, {"payment_token": "tok_1"},
                     {"billing_address": "somewhere"}, {"payment_method_id": "pm_1"}):
        with pytest.raises(ValidationError):
            McpCardCheckoutRequest(bundle_code="BUNDLE-1", **smuggled)


def test_card_request_cannot_assert_an_amount_or_an_outcome():
    """(7)(15) No price, currency, url or "paid" flag can be supplied by the client."""
    for smuggled in ({"amount": 1}, {"currency": "EUR"}, {"paid": True}, {"status": "COMPLETED"},
                     {"checkout_url": "https://evil.test"}, {"order_id": "order-9"},
                     {"user_id": "somebody-else"}, {"payment_type": "Card"}):
        with pytest.raises(ValidationError):
            McpCardCheckoutRequest(bundle_code="BUNDLE-1", **smuggled)


def test_card_service_holds_no_card_vocabulary():
    """The module has no code path that could read, store or forward a card detail."""
    text = executable_source(CARD_SERVICE_SOURCE).lower()
    for forbidden in ("card_number", "cvv", "cvc", "cardholder", "payment_method_id", "pan "):
        assert forbidden not in text


# --- (13)(14) Delegation and the returned link ---------------------------------------


@pytest.mark.asyncio
async def test_card_checkout_returns_the_generated_secure_link(user, card_service, stripe_session,
                                                                order_repo):
    """(14) The link the backend generated is what comes back, with a safe field set."""
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=stripe_session) as hosted:
        result = await card_service.create_checkout(user=user, request=a_request(),
                                                    x_currency="USD", device_id="device-1")

    hosted.assert_called_once()
    assert result.data.checkout_url == "https://checkout.stripe.test/pay/cs_test_1"
    assert result.data.payment_reference == "order-1"
    assert result.data.order_id == "order-1"
    assert result.data.amount == "10.00"
    assert result.data.currency == "USD"
    assert result.data.expires_at is not None
    # The order row is created through the existing repository, once.
    order_repo.create.assert_called_once()


@pytest.mark.asyncio
async def test_card_checkout_reuses_the_existing_bundle_source_and_prices_server_side(user, card_service,
                                                                                       hub_service,
                                                                                       stripe_session,
                                                                                       order_repo):
    """(9) The plan and its price come from the platform, never from the request."""
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=stripe_session) as hosted:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="device-1")

    hub_service.get_bundle_by_id.assert_awaited_once_with(bundle_id="BUNDLE-1")
    created = order_repo.create.call_args[0][0]
    # 10.00 USD held in the existing minor-unit columns, in the system currency.
    assert created["amount"] == 1000
    assert created["modified_amount"] == 1000
    assert created["currency"] == "USD"
    assert created["user_id"] == "user-1"
    assert created["payment_type"] == "Card"
    assert hosted.call_args.kwargs["amount"] == 1000


@pytest.mark.asyncio
async def test_card_checkout_refuses_an_unavailable_bundle_before_creating_anything(user, card_service,
                                                                                     hub_service,
                                                                                     order_repo):
    hub_service.get_bundle_by_id = AsyncMock(return_value=None)

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 400
    assert raised.value.name == ErrorMessages.BUNDLE_NOT_AVAILABLE
    order_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_card_checkout_refuses_an_inactive_bundle(user, card_service, hub_service, bundle,
                                                         order_repo):
    bundle.is_active = False

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.name == ErrorMessages.BUNDLE_NOT_AVAILABLE
    order_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_card_checkout_checks_applicability_of_a_non_stockable_bundle(user, card_service,
                                                                             hub_service, bundle,
                                                                             order_repo):
    """The same second availability check the shared assign flow performs."""
    bundle.is_stockable = False
    hub_service.check_bundle_applicable = AsyncMock(return_value=False)

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.name == ErrorMessages.BUNDLE_NOT_AVAILABLE
    order_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_card_checkout_fails_before_stripe_when_the_order_cannot_be_recorded(user, card_service,
                                                                                    order_repo):
    """Without a persisted order there is nothing for the webhook to fulfil."""
    order_repo.create.return_value = None

    with patch("app.services.mcp_card_service.create_hosted_checkout_session") as hosted, \
            pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 503
    hosted.assert_not_called()


# --- (15) Creating a link never reports payment success ------------------------------


@pytest.mark.asyncio
async def test_card_checkout_reports_pending_and_charges_nothing(user, card_service, stripe_session,
                                                                  order_repo):
    """(15) Nothing is paid, nothing is provisioned, and no payment status is written."""
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=stripe_session):
        result = await card_service.create_checkout(user=user, request=a_request(),
                                                    x_currency="USD", device_id="d")

    assert result.data.status == "PENDING"
    assert result.data.next_action == "OPEN_CHECKOUT_URL"
    # The only write to the order is the provider reference, on the existing column.
    order_repo.update_by.assert_called_once_with({"id": "order-1"},
                                                 data={"payment_intent_code": "pi_test_1"})
    for call in order_repo.update_by.call_args_list:
        assert "payment_status" not in (call.kwargs.get("data") or {})
        assert "order_status" not in (call.kwargs.get("data") or {})


def test_card_service_never_provisions_or_debits_a_wallet():
    """(12)(13) The adapter contains no provisioning and no wallet code."""
    text = executable_source(CARD_SERVICE_SOURCE)
    for forbidden in ("buy_bundle", "top_up_bundle", "create_reseller_order",
                      "add_wallet_transaction", "UserWalletService", "handle_payment_webhook"):
        assert forbidden not in text


# --- The link is validated, and one page means one Stripe object ---------------------


@pytest.mark.asyncio
async def test_card_checkout_refuses_a_session_without_a_usable_link(user, card_service):
    """A page the user cannot open is never reported as a checkout."""
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=SimpleNamespace(id="cs_1", url=None, payment_intent="pi_1", expires_at=None)), \
            pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 503
    assert raised.value.name == "MCP_CARD_CHECKOUT_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_url", [
    "javascript:alert(1)",
    "data:text/html,<script>",
    "http://checkout.stripe.test/pay/cs_1",
    "https://user:pass@checkout.stripe.test/pay",
    "https:// checkout.stripe.test/pay",
])
async def test_card_checkout_refuses_an_unsafe_link(user, card_service, unsafe_url):
    """Refused rather than repaired: a link is the one value a user is asked to act on."""
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=SimpleNamespace(id="cs_1", url=unsafe_url, payment_intent="pi_1",
                                            expires_at=None)), \
            pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 503


@pytest.mark.asyncio
async def test_card_checkout_does_not_also_create_a_legacy_payment_intent(user, card_service,
                                                                          stripe_session):
    """One checkout, one provider object. No abandoned PaymentIntent is left behind."""
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=stripe_session), \
         patch("app.config.utils.create_payment_intent") as legacy_intent:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    legacy_intent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,expected_minutes", [
    (None, 31),          # default
    ("5", 31),           # below Stripe's floor, clamped up
    ("2000", 24 * 60 - 1),  # above Stripe's ceiling, clamped down
    ("not-a-number", 31),   # unparseable, falls back to the default
    ("60", 60),
])
async def test_card_checkout_clamps_the_page_lifetime_to_the_provider_window(user, card_service,
                                                                             stripe_session,
                                                                             monkeypatch, configured,
                                                                             expected_minutes):
    """An out-of-range expiry would be refused by the provider, so it is clamped here."""
    import time

    if configured is None:
        monkeypatch.delenv("MCP_CARD_SESSION_EXPIRY_MINUTES", raising=False)
    else:
        monkeypatch.setenv("MCP_CARD_SESSION_EXPIRY_MINUTES", configured)

    before = int(time.time())
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=stripe_session) as hosted:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    delta_minutes = (hosted.call_args.kwargs["expires_at"] - before) / 60
    assert expected_minutes - 1 <= delta_minutes <= expected_minutes + 1


@pytest.mark.asyncio
async def test_card_checkout_keys_the_provider_call_on_the_durable_order_id(user, card_service,
                                                                             stripe_session):
    """Stripe idempotency is keyed on a persisted primary key, not on process state."""
    with patch("app.services.mcp_card_service.create_hosted_checkout_session",
               return_value=stripe_session) as hosted:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert hosted.call_args.kwargs["idempotency_key"] == "mcp-card-order-1"


# --- Fail-closed configuration and flags ---------------------------------------------


@pytest.mark.asyncio
async def test_card_checkout_is_disabled_by_its_own_flag(user, card_service, order_repo, monkeypatch):
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "false")

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 503
    assert raised.value.name == "MCP_CARD_PURCHASE_DISABLED"
    order_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_card_checkout_is_disabled_by_the_master_flag(user, card_service, order_repo, monkeypatch):
    """Card checkout is a subset of MCP purchasing; it cannot be enabled on its own."""
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 503
    order_repo.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["MCP_CARD_SUCCESS_URL", "MCP_CARD_CANCEL_URL"])
async def test_card_checkout_fails_closed_on_missing_redirect_configuration(user, card_service,
                                                                            order_repo, monkeypatch,
                                                                            missing):
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 503
    assert raised.value.name == "MCP_CARD_CONFIG_INVALID"
    order_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_card_checkout_refuses_a_plain_http_redirect_target(user, card_service, monkeypatch):
    monkeypatch.setenv("MCP_CARD_SUCCESS_URL", "http://shop.example.test/paid")

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.name == "MCP_CARD_CONFIG_INVALID"


@pytest.mark.asyncio
async def test_card_checkout_refuses_an_unsupported_currency(user, card_service, order_repo):
    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=user, request=a_request(), x_currency="EUR",
                                           device_id="d")

    assert raised.value.name == "MCP_UNSUPPORTED_CURRENCY"
    order_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_card_checkout_refuses_an_anonymous_session(card_service, order_repo):
    from app.models.user import UserModel

    guest = UserModel(id="anon-1", email="", token="t", msisdn=None, is_verified=False, is_anonymous=True)

    with pytest.raises(CustomException) as raised:
        await card_service.create_checkout(user=guest, request=a_request(), x_currency="USD",
                                           device_id="d")

    assert raised.value.code == 401
    order_repo.create.assert_not_called()


# --- (21)(22) Status polling ----------------------------------------------------------


def test_card_status_reads_existing_records_and_mutates_nothing(user, card_service, order_repo,
                                                                 profile_repo):
    """(21) A status read touches no writer at all."""
    order_repo.get_first_by.return_value = make_order(payment_status=OrderStatusEnum.SUCCESS,
                                                      order_status=OrderStatusEnum.SUCCESS)
    profile_repo.get_first_by.return_value = SimpleNamespace(id="profile-1")

    result = card_service.get_status(user=user, payment_reference="order-1")

    assert result.data.status == "COMPLETED"
    assert result.data.paid is True
    assert result.data.provisioned is True
    assert result.data.order_id == "order-1"
    assert result.data.next_action == "GET_ESIM_BY_ORDER"
    order_repo.create.assert_not_called()
    order_repo.update_by.assert_not_called()


def test_card_status_scopes_the_lookup_to_the_authenticated_user(user, card_service, order_repo):
    """(22) Ownership is part of the query, not a check afterwards."""
    order_repo.get_first_by.return_value = None

    with pytest.raises(CustomException) as raised:
        card_service.get_status(user=user, payment_reference="somebody-elses-order")

    assert raised.value.code == 404
    assert raised.value.name == "MCP_PAYMENT_NOT_FOUND"
    assert order_repo.get_first_by.call_args[0][0] == {"id": "somebody-elses-order", "user_id": "user-1"}


def test_card_status_of_another_users_payment_is_indistinguishable_from_absent(user, other_user,
                                                                               card_service, order_repo):
    """A foreign order and a non-existent one produce the same answer."""

    def only_own(where):
        return make_order(user_id="user-1") if where.get("user_id") == "user-1" else None

    order_repo.get_first_by.side_effect = only_own

    assert card_service.get_status(user=user, payment_reference="order-1").data.order_id == "order-1"
    with pytest.raises(CustomException) as raised:
        card_service.get_status(user=other_user, payment_reference="order-1")
    assert raised.value.code == 404


@pytest.mark.parametrize("payment_status,order_status,provisioned,expected", [
    (OrderStatusEnum.PENDING, OrderStatusEnum.PENDING, False, "PENDING"),
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.PENDING, False, "PROVISIONING"),
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.SUCCESS, False, "PROVISIONING"),
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.SUCCESS, True, "COMPLETED"),
    (OrderStatusEnum.SUCCESS, OrderStatusEnum.FAILURE, False, "AMBIGUOUS"),
    (OrderStatusEnum.FAILURE, OrderStatusEnum.PENDING, False, "FAILED"),
    (OrderStatusEnum.PENDING, OrderStatusEnum.FAILURE, False, "FAILED"),
    (OrderStatusEnum.CANCELED, OrderStatusEnum.CANCELED, False, "CANCELLED"),
    (OrderStatusEnum.PENDING, OrderStatusEnum.CANCELED, False, "CANCELLED"),
])
def test_card_status_is_derived_from_existing_order_columns(user, card_service, order_repo, profile_repo,
                                                             payment_status, order_status, provisioned,
                                                             expected):
    """No provider call, no redirect and no client assertion takes part in this."""
    order_repo.get_first_by.return_value = make_order(payment_status=payment_status,
                                                      order_status=order_status)
    profile_repo.get_first_by.return_value = SimpleNamespace(id="p") if provisioned else None

    result = card_service.get_status(user=user, payment_reference="order-1")

    assert result.data.status == expected
    # "paid" and "provisioned" are never collapsed into one another.
    assert result.data.provisioned is provisioned


def test_card_status_never_reports_paid_without_a_successful_payment_row(user, card_service,
                                                                          order_repo, profile_repo):
    order_repo.get_first_by.return_value = make_order(payment_status=OrderStatusEnum.PENDING,
                                                      order_status=OrderStatusEnum.PENDING)
    profile_repo.get_first_by.return_value = None

    assert card_service.get_status(user=user, payment_reference="order-1").data.paid is False


def test_card_status_is_gated_by_the_flag(user, card_service, monkeypatch):
    monkeypatch.setenv("MCP_CARD_PURCHASE_ENABLED", "false")

    with pytest.raises(CustomException) as raised:
        card_service.get_status(user=user, payment_reference="order-1")

    assert raised.value.code == 503


def test_card_status_response_carries_no_provider_internals(user, card_service, order_repo,
                                                             profile_repo):
    """No session id, client secret, publishable key or intent id reaches the client."""
    order_repo.get_first_by.return_value = make_order(payment_status=OrderStatusEnum.SUCCESS,
                                                      order_status=OrderStatusEnum.SUCCESS)
    profile_repo.get_first_by.return_value = SimpleNamespace(id="p")

    fields = set(card_service.get_status(user=user, payment_reference="order-1").data.model_dump())

    for leaked in ("payment_intent_code", "client_secret", "publishable_key", "session_id",
                   "customer_id", "ephemeral_key", "otp", "token"):
        assert leaked not in fields
