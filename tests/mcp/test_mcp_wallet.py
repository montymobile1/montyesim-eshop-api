"""The MCP wallet adapter delegates; it does not reimplement.

Each test pins one property the adapter has to hold: the existing purchase flow is the
only thing that buys anything, the caller comes from the verified token, the price and the
bundle are decided server-side, and no outcome is reported more favourably than the order
row the shared flow actually wrote.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.config.constants import ErrorMessages
from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.exceptions import CustomException
from app.schemas.bundle import PaymentIntentResponse
from app.schemas.mcp import McpAssignRequest, McpCountryRequest, McpRelatedSearchRequest
from app.schemas.response import ResponseHelper
from tests.mcp.conftest import executable_source, make_order


def completed_order():
    return make_order(payment_status=OrderStatusEnum.SUCCESS, order_status=OrderStatusEnum.SUCCESS,
                      payment_type=PaymentTypeEnum.WALLET)


# --- (10) MCP Wallet delegates to the existing Wallet purchase flow ------------------


@pytest.mark.asyncio
async def test_wallet_delegates_to_the_existing_assign_flow(user, purchase_service, assign_flow,
                                                            order_repo, profile_repo):
    """The adapter calls ``UserBundleService.assign`` once, with the legacy request shape."""
    order_repo.get_first_by.return_value = completed_order()
    profile_repo.get_first_by.return_value = SimpleNamespace(id="profile-1")

    result = await purchase_service.assign_wallet_bundle(
        user=user, device_id="device-1", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
        x_currency="USD", locale="en", request=MagicMock())

    assign_flow.assign.assert_awaited_once()
    kwargs = assign_flow.assign.await_args.kwargs
    assert kwargs["assign_request"].payment_type == PaymentTypeEnum.WALLET
    assert kwargs["assign_request"].bundle_code == "BUNDLE-1"
    # Promotions and affiliate codes are not part of the MCP surface.
    assert kwargs["assign_request"].promo_code is None
    assert kwargs["assign_request"].affiliate_code is None
    # The shared flow serializes related_search unconditionally, so it is never None.
    assert kwargs["assign_request"].related_search is not None
    assert result.data.status == "COMPLETED"
    assert result.data.order_id == "order-1"


@pytest.mark.asyncio
async def test_wallet_passes_the_token_user_and_never_a_body_field(user, purchase_service,
                                                                   order_repo, profile_repo, assign_flow):
    """(8) The user handed to the shared flow is the one the verified token produced."""
    order_repo.get_first_by.return_value = completed_order()
    profile_repo.get_first_by.return_value = SimpleNamespace(id="profile-1")

    await purchase_service.assign_wallet_bundle(
        user=user, device_id="device-1", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
        x_currency="USD", locale="en", request=MagicMock())

    assert assign_flow.assign.await_args.kwargs["user"] is user
    # And the order is read back scoped to that same user.
    assert order_repo.get_first_by.call_args[0][0]["user_id"] == "user-1"


def test_wallet_request_cannot_carry_an_identity_or_a_price():
    """(8)(9) There is no field through which any of these could arrive."""
    for smuggled in ({"user_id": "somebody-else"}, {"amount": 1}, {"currency": "EUR"},
                     {"balance": 999}, {"order_id": "order-9"}, {"paid": True},
                     {"payment_status": "COMPLETED"}, {"checkout_url": "https://evil.test"}):
        with pytest.raises(ValidationError):
            McpAssignRequest(bundle_code="BUNDLE-1", **smuggled)


def test_wallet_request_refuses_a_non_wallet_payment_type():
    """This endpoint is the wallet adapter; it never silently re-routes to another method."""
    with pytest.raises(ValidationError):
        McpAssignRequest(bundle_code="BUNDLE-1", payment_type="Card")


@pytest.mark.asyncio
async def test_wallet_translates_related_search_into_the_internal_dto(user, purchase_service,
                                                                      assign_flow, order_repo, profile_repo):
    """Pure shape translation; the value is descriptive and never prices anything."""
    order_repo.get_first_by.return_value = completed_order()
    profile_repo.get_first_by.return_value = SimpleNamespace(id="profile-1")

    await purchase_service.assign_wallet_bundle(
        user=user, device_id="d",
        mcp_request=McpAssignRequest(
            bundle_code="BUNDLE-1",
            related_search=McpRelatedSearchRequest(
                countries=[McpCountryRequest(iso3_code="FRA", country_name="France")])),
        x_currency="USD", locale="en", request=MagicMock())

    related = assign_flow.assign.await_args.kwargs["assign_request"].related_search
    assert related.countries[0].iso3_code == "FRA"
    assert related.region is None


# --- (9) Bundle availability and price are checked server-side ----------------------


@pytest.mark.asyncio
async def test_wallet_bundle_availability_and_price_come_from_the_shared_flow(user, purchase_service,
                                                                              assign_flow):
    """The adapter never prices or validates a bundle itself.

    An unavailable bundle is refused inside ``assign`` -- the same check the website hits --
    and the adapter simply lets the platform's own error through.
    """
    assign_flow.assign = AsyncMock(side_effect=CustomException(
        code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE, details=ErrorMessages.BUNDLE_NOT_AVAILABLE))

    with pytest.raises(CustomException) as raised:
        await purchase_service.assign_wallet_bundle(
            user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="GONE"),
            x_currency="USD", locale="en", request=MagicMock())

    assert raised.value.code == 400
    assert raised.value.name == ErrorMessages.BUNDLE_NOT_AVAILABLE


def test_wallet_adapter_holds_no_pricing_wallet_or_provisioning_code():
    """(7) The adapter module does not reimplement any of the flows it delegates to."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "services" / "mcp_purchase_service.py"
    text = executable_source(source)
    for forbidden in ("add_wallet_transaction", "buy_bundle", "top_up_bundle", "create_reseller_order",
                      "PaymentIntent", "stripe"):
        assert forbidden not in text, f"{forbidden} must not appear in the MCP wallet adapter"


# --- (11)(12) Existing order, provisioning and insufficient-balance behaviour --------


@pytest.mark.asyncio
async def test_wallet_reports_completed_only_when_the_order_row_confirms_it(user, purchase_service,
                                                                            order_repo, profile_repo):
    """(11) Success is read from the order and profile the shared flow wrote."""
    order_repo.get_first_by.return_value = completed_order()
    profile_repo.get_first_by.return_value = SimpleNamespace(id="profile-1")

    result = await purchase_service.assign_wallet_bundle(
        user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
        x_currency="USD", locale="en", request=MagicMock())

    assert result.responseCode == 200
    assert result.data.status == "COMPLETED"
    assert result.data.provisioning_status == "COMPLETED"
    assert result.data.next_action == "GET_ESIM_BY_ORDER"
    assert result.data.payment_method == "Wallet"
    # Provisioning is looked up against the order the shared flow created.
    assert profile_repo.get_first_by.call_args[0][0] == {"user_order_id": "order-1", "user_id": "user-1"}


@pytest.mark.asyncio
async def test_wallet_escalates_when_charged_without_an_esim(user, purchase_service,
                                                             order_repo, profile_repo):
    """Money moved and no eSIM exists: never success, never a retryable failure.

    The shared wallet branch answers ``COMPLETED`` here (its provisioning helper *returns*
    its error rather than raising), which the website depends on and which is left alone.
    The MCP answer is derived from the order row instead.
    """
    order_repo.get_first_by.return_value = make_order(payment_status=OrderStatusEnum.SUCCESS,
                                                      order_status=OrderStatusEnum.FAILURE)
    profile_repo.get_first_by.return_value = None

    result = await purchase_service.assign_wallet_bundle(
        user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
        x_currency="USD", locale="en", request=MagicMock())

    assert result.responseCode == 424
    assert result.status == "failed"
    assert result.data.status == "MANUAL_INTERVENTION_REQUIRED"
    assert result.data.next_action == "CONTACT_SUPPORT"
    # The order id survives, because it is what support needs.
    assert result.data.order_id == "order-1"


@pytest.mark.asyncio
async def test_wallet_reports_failure_when_the_order_failed(user, purchase_service,
                                                            order_repo, profile_repo):
    order_repo.get_first_by.return_value = make_order(payment_status=OrderStatusEnum.FAILURE,
                                                      order_status=OrderStatusEnum.FAILURE)

    result = await purchase_service.assign_wallet_bundle(
        user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
        x_currency="USD", locale="en", request=MagicMock())

    assert result.data.status == "FAILED"
    assert result.data.provisioning_status is None


@pytest.mark.asyncio
async def test_wallet_insufficient_balance_behaviour_is_preserved(user, purchase_service, assign_flow):
    """(12) The platform's own insufficient-balance error reaches the MCP client unchanged."""
    assign_flow.assign = AsyncMock(side_effect=CustomException(
        code=400, name=ErrorMessages.INSUFFICIENT_WALLET_BALANCE,
        details="Insufficient wallet balance, please top up your wallet"))

    with pytest.raises(CustomException) as raised:
        await purchase_service.assign_wallet_bundle(
            user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
            x_currency="USD", locale="en", request=MagicMock())

    assert raised.value.code == 400
    assert raised.value.name == ErrorMessages.INSUFFICIENT_WALLET_BALANCE


@pytest.mark.asyncio
async def test_wallet_never_claims_an_idempotent_replay(user, purchase_service, order_repo, profile_repo):
    """No MCP persistence exists, so no replay is claimed."""
    order_repo.get_first_by.return_value = completed_order()
    profile_repo.get_first_by.return_value = SimpleNamespace(id="p")

    result = await purchase_service.assign_wallet_bundle(
        user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
        x_currency="USD", locale="en", request=MagicMock())

    assert result.data.idempotent_replay is False


@pytest.mark.asyncio
async def test_wallet_refuses_a_currency_it_cannot_settle(user, purchase_service, assign_flow):
    """Refused before anything is created; a silent substitution would misprice the sale."""
    with pytest.raises(CustomException) as raised:
        await purchase_service.assign_wallet_bundle(
            user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
            x_currency="EUR", locale="en", request=MagicMock())

    assert raised.value.code == 400
    assert raised.value.name == "MCP_UNSUPPORTED_CURRENCY"
    assign_flow.assign.assert_not_awaited()


@pytest.mark.asyncio
async def test_wallet_refuses_an_anonymous_session(purchase_service, assign_flow):
    """A guest session may buy on the website; it may never buy through MCP."""
    from app.models.user import UserModel

    guest = UserModel(id="anon-1", email="", token="t", msisdn=None, is_verified=False, is_anonymous=True)

    with pytest.raises(CustomException) as raised:
        await purchase_service.assign_wallet_bundle(
            user=guest, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
            x_currency="USD", locale="en", request=MagicMock())

    assert raised.value.code == 401
    assign_flow.assign.assert_not_awaited()


@pytest.mark.asyncio
async def test_wallet_escalates_when_the_order_cannot_be_read_back(user, purchase_service,
                                                                    order_repo, profile_repo):
    """An unreadable outcome after a possible debit is escalated, never called a failure."""
    order_repo.get_first_by.return_value = None
    profile_repo.get_first_by.return_value = None

    result = await purchase_service.assign_wallet_bundle(
        user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
        x_currency="USD", locale="en", request=MagicMock())

    assert result.responseCode == 424
    assert result.data.status == "MANUAL_INTERVENTION_REQUIRED"


@pytest.mark.asyncio
async def test_wallet_refuses_an_answer_without_an_order_reference(user, purchase_service, assign_flow):
    """Without an order id there is nothing to confirm, so nothing is claimed."""
    assign_flow.assign = AsyncMock(
        return_value=ResponseHelper.success_data_response(PaymentIntentResponse(order_id=""), 0))

    with pytest.raises(CustomException) as raised:
        await purchase_service.assign_wallet_bundle(
            user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
            x_currency="USD", locale="en", request=MagicMock())

    assert raised.value.code == 502


# --- (5) The feature flag ------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_endpoint_is_disabled_by_its_flag(user, purchase_service, assign_flow, monkeypatch):
    monkeypatch.setenv("MCP_PURCHASE_ENABLED", "false")

    with pytest.raises(CustomException) as raised:
        await purchase_service.assign_wallet_bundle(
            user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
            x_currency="USD", locale="en", request=MagicMock())

    assert raised.value.code == 503
    assert raised.value.name == "MCP_PURCHASE_DISABLED"
    assign_flow.assign.assert_not_awaited()


@pytest.mark.asyncio
async def test_wallet_flag_fails_closed_when_unset(user, purchase_service, assign_flow, monkeypatch):
    monkeypatch.delenv("MCP_PURCHASE_ENABLED", raising=False)

    with pytest.raises(CustomException) as raised:
        await purchase_service.assign_wallet_bundle(
            user=user, device_id="d", mcp_request=McpAssignRequest(bundle_code="BUNDLE-1"),
            x_currency="USD", locale="en", request=MagicMock())

    assert raised.value.code == 503
    assign_flow.assign.assert_not_awaited()
