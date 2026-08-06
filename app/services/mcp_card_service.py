"""The MCP card adapter: open a hosted payment page, and read what happened to it.

What this service does **not** do is the important part:

* it never takes a card. There is no parameter anywhere in this module for a card number,
  an expiry, a security code, a cardholder or a payment token. The card is entered on the
  provider's own hosted page, which this backend never renders and never proxies;
* it never marks anything paid. Creating a page leaves the order exactly as pending as the
  legacy card flow leaves it, and no code path here writes a payment status;
* it never provisions. The existing signature-verified Stripe webhook
  (``CallbackService.handle_payment_webhook``) remains the single payment authority and the
  single provisioning trigger. It is not modified by this branch at all;
* it adds no persistence. The order row it creates is the existing ``user_order`` row with
  the existing columns, written through the existing repository. ``payment_reference`` *is*
  ``user_order.id``, which is why status polling needs no new table.

Everything it does reuse comes from the same places the legacy card flow uses: the eSIM
hub client for the bundle, ``CurrencyService`` for the rate, ``UserOrderRepo`` for the
order row, and the module-level Stripe client configured in :mod:`app.config.utils`.
"""

import os
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

from app.config.config import esim_hub_service_instance
from app.config.constants import ErrorMessages
from app.config.db import PaymentTypeEnum
from app.config.mcp_constants import (
    DEFAULT_CARD_SESSION_EXPIRY_MINUTES,
    MAX_CARD_SESSION_EXPIRY_MINUTES,
    MCP_CARD_CANCEL_URL_ENV,
    MCP_CARD_SESSION_EXPIRY_MINUTES_ENV,
    MCP_CARD_STRIPE_IDEMPOTENCY_PREFIX,
    MCP_CARD_SUCCESS_URL_ENV,
    MCP_SOURCE_MARKER,
    MCP_SOURCE_METADATA_KEY,
    MIN_CARD_SESSION_EXPIRY_MINUTES,
    McpCardStatus,
    McpErrorMessages,
    McpNextAction,
)
from app.config.utils import create_hosted_checkout_session
from app.exceptions import CustomException
from app.models.user import UserModel, UserOrderModel, UserOrderType
from app.repo import UserOrderRepo, UserProfileRepo
from app.schemas.mcp_card import McpCardCheckoutRequest, McpCardCheckoutResponse, McpCardStatusResponse
from app.schemas.response import Response, ResponseHelper
from app.services.currency_service import CurrencyService
from app.services.mcp_common import (
    ensure_authenticated_user,
    ensure_mcp_card_purchase_enabled,
    ensure_supported_currency,
    minor_units_to_decimal_string,
    order_did_not_succeed,
    order_is_cancelled,
    order_is_failed,
    order_is_paid,
    order_succeeded,
    to_related_search,
)

#: Hosts for which plain http is tolerated, so the flow can be run locally.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class McpCardCheckoutService:
    """Open one hosted payment page per request, and read one payment's state."""

    def __init__(
        self,
        esim_hub_service=None,
        user_order_repo: Optional[UserOrderRepo] = None,
        user_profile_repo: Optional[UserProfileRepo] = None,
        currency_service: Optional[CurrencyService] = None,
    ):
        self.__esim_hub_service = esim_hub_service if esim_hub_service is not None else esim_hub_service_instance()
        self.__user_order_repo = user_order_repo if user_order_repo is not None else UserOrderRepo()
        self.__user_profile_repo = user_profile_repo if user_profile_repo is not None else UserProfileRepo()
        self.__currency_service = currency_service if currency_service is not None else CurrencyService()

    async def create_checkout(
        self,
        user: UserModel,
        request: McpCardCheckoutRequest,
        x_currency: str,
        device_id: str,
    ) -> Response[McpCardCheckoutResponse]:
        """Create the pending order and the hosted page. Charges nothing."""
        ensure_mcp_card_purchase_enabled()
        user = ensure_authenticated_user(user)
        currency = ensure_supported_currency(x_currency)

        # Validated before anything is created, so a misconfigured deployment fails with a
        # clear code rather than handing the provider a bad redirect target.
        success_url = self.__validated_redirect_url(MCP_CARD_SUCCESS_URL_ENV)
        cancel_url = self.__validated_redirect_url(MCP_CARD_CANCEL_URL_ENV)

        # The bundle is re-read from the platform's own source and re-checked here, exactly
        # as the shared assign flow re-checks it. Nothing about the plan, its availability
        # or its price is taken from the MCP request.
        bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id=request.bundle_code)
        if not bundle or not bundle.is_active:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                  details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        if not bundle.is_stockable:
            available = await self.__esim_hub_service.check_bundle_applicable(bundle.bundle_info_code)
            if not available:
                raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                      details=ErrorMessages.BUNDLE_NOT_AVAILABLE)

        # Server-side price, in the same units and the same columns the legacy card flow
        # writes. There is no amount in the MCP request to compare against or trust.
        amount = bundle.original_price
        related_search = to_related_search(request.related_search)

        order: UserOrderModel = self.__user_order_repo.create({
            "user_id": user.id,
            "bundle_id": request.bundle_code,
            "order_type": UserOrderType.ASSIGN,
            "amount": int(round(amount * 100)),
            "modified_amount": int(round(amount * 100)),
            "currency": "USD",
            "bundle_data": bundle.model_dump_json(),
            "searched_countries": related_search.model_dump_json(),
            "anonymous_user_id": user.anonymous_user_id,
            "promo_code": None,
            "payment_type": PaymentTypeEnum.CARD,
        })
        if order is None or not order.id:
            # Without a persisted order there is no reference to key the provider call on,
            # nothing for the webhook to fulfil and nothing to poll. Fail before Stripe.
            raise CustomException(code=503, name=McpErrorMessages.MCP_DEPENDENCY_UNAVAILABLE,
                                  details="The order could not be recorded. Nothing was charged.")

        raw_rate = self.__currency_service.get_currency_rate("USD", to_currency=currency)
        order_amount_cents = Decimal(str(order.modified_amount if order.modified_amount else order.amount))
        stripe_amount = int((order_amount_cents * Decimal(str(raw_rate))).quantize(Decimal("1"),
                                                                                   rounding=ROUND_HALF_UP))

        # The same keys ``__handle_card_payment`` writes, so the existing webhook handler
        # -- which indexes order_id/user_id/bundle_code directly and filters on env --
        # fulfils this payment through the branch it already uses. ``mcp_source`` is a
        # support-triage marker the existing handler never reads.
        metadata = {
            "order_id": order.id,
            "user_id": order.user_id,
            "device_id": device_id or "",
            "bundle_code": order.bundle_id,
            "order_type": str(order.order_type),
            "env": os.environ.get("ENVIRONMENT", "DEV"),
            "rule_id": "0",
            "amount": str(stripe_amount),
            MCP_SOURCE_METADATA_KEY: MCP_SOURCE_MARKER,
        }

        expires_at = self.__session_expires_at()
        session = create_hosted_checkout_session(
            user_email=user.email,
            amount=stripe_amount,
            currency=currency.lower(),
            product_name=bundle.bundle_name or bundle.bundle_code,
            metadata=metadata,
            success_url=success_url,
            cancel_url=cancel_url,
            expires_at=expires_at,
            # Keyed on the database-generated order id, which is already persisted, so no
            # MCP-specific storage is needed to make the provider call repeatable.
            idempotency_key=f"{MCP_CARD_STRIPE_IDEMPOTENCY_PREFIX}{order.id}",
            description=f"Bundle order ({order.order_type}) for bundle {order.bundle_id}",
        )

        payment_intent_code = self.__intent_id(session)
        if payment_intent_code:
            # The existing column, and the only write this method makes to the order. No
            # payment status is set here: only the webhook may move a payment forward.
            self.__user_order_repo.update_by({"id": order.id}, data={"payment_intent_code": payment_intent_code})

        checkout_url = self.__safe_checkout_url(getattr(session, "url", None))
        if not checkout_url:
            logger.error(f"hosted checkout session for order {order.id} carried no usable url")
            raise CustomException(
                code=503,
                name=McpErrorMessages.MCP_CARD_CHECKOUT_UNAVAILABLE,
                details="A payment page could not be opened. Nothing was charged.",
            )

        payload = McpCardCheckoutResponse(
            payment_reference=order.id,
            order_id=order.id,
            checkout_url=checkout_url,
            status=McpCardStatus.PENDING,
            amount=minor_units_to_decimal_string(stripe_amount),
            currency=currency,
            expires_at=self.__isoformat(getattr(session, "expires_at", None) or expires_at),
            next_action=McpNextAction.OPEN_CHECKOUT_URL,
            idempotent_replay=False,
            message="A secure payment page was opened for this plan. Nothing has been charged yet.",
        )
        return ResponseHelper.success_data_response(payload, 0)

    def get_status(self, user: UserModel, payment_reference: str) -> Response[McpCardStatusResponse]:
        """Read one payment's state. Mutates nothing and provisions nothing.

        Ownership is part of the lookup rather than a check afterwards, so an order that
        belongs to somebody else is indistinguishable from one that never existed.
        """
        ensure_mcp_card_purchase_enabled()
        user = ensure_authenticated_user(user)

        order: Optional[UserOrderModel] = self.__user_order_repo.get_first_by(
            {"id": payment_reference, "user_id": user.id})
        if order is None:
            raise CustomException(code=404, name=McpErrorMessages.MCP_PAYMENT_NOT_FOUND,
                                  details="No payment with that reference belongs to this account")

        provisioned = self.__user_profile_repo.get_first_by(
            {"user_order_id": order.id, "user_id": user.id}) is not None
        status = self.__map_status(order=order, provisioned=provisioned)

        payload = McpCardStatusResponse(
            payment_reference=str(order.id),
            status=status,
            order_id=str(order.id),
            amount=minor_units_to_decimal_string(
                order.modified_amount if order.modified_amount else order.amount),
            currency=order.currency,
            bundle_code=order.bundle_id,
            quote_reference=None,
            expires_at=None,
            paid=status in (McpCardStatus.PAID, McpCardStatus.PROVISIONING, McpCardStatus.COMPLETED),
            provisioned=provisioned,
            next_action=self.__next_action_for(status),
            message=None,
        )
        return ResponseHelper.success_data_response(payload, 0)

    @staticmethod
    def __map_status(order: UserOrderModel, provisioned: bool) -> McpCardStatus:
        """Derive the reported status from existing order columns only.

        No provider call, no redirect, no chatbot assertion and no stored MCP state takes
        part in this decision.
        """
        payment_status = order.payment_status
        order_status = order.order_status

        if order_is_cancelled(payment_status) or order_is_cancelled(order_status):
            return McpCardStatus.CANCELLED
        if order_is_paid(payment_status):
            if order_did_not_succeed(order_status):
                # Money arrived and the order failed. The platform cannot say the customer
                # got what they paid for, so it says so rather than guessing.
                return McpCardStatus.AMBIGUOUS
            if order_succeeded(order_status) and provisioned:
                return McpCardStatus.COMPLETED
            return McpCardStatus.PROVISIONING
        if order_is_failed(payment_status) or order_did_not_succeed(order_status):
            return McpCardStatus.FAILED
        return McpCardStatus.PENDING

    @staticmethod
    def __next_action_for(status: McpCardStatus) -> Optional[McpNextAction]:
        if status is McpCardStatus.COMPLETED:
            return McpNextAction.GET_ESIM_BY_ORDER
        if status is McpCardStatus.PENDING:
            return McpNextAction.OPEN_CHECKOUT_URL
        if status in (McpCardStatus.FAILED, McpCardStatus.EXPIRED, McpCardStatus.CANCELLED):
            return McpNextAction.NEW_CHECKOUT_REQUIRED
        if status is McpCardStatus.AMBIGUOUS:
            return McpNextAction.CONTACT_SUPPORT
        return None

    @staticmethod
    def __intent_id(session) -> Optional[str]:
        """The PaymentIntent the Session created, whether expanded or not."""
        intent = getattr(session, "payment_intent", None)
        if intent is None:
            return None
        if isinstance(intent, str):
            return intent
        return getattr(intent, "id", None)

    @staticmethod
    def __safe_checkout_url(value: Optional[str]) -> Optional[str]:
        """Return the link only if it is a plain https address with a host.

        Refused rather than repaired: this is the one value a user is asked to act on.
        """
        if not value:
            return None
        candidate = str(value).strip()
        if not candidate or any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
            return None
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return None
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return candidate

    @staticmethod
    def __validated_redirect_url(env_name: str) -> str:
        """An absolute http(s) redirect target from ``env_name``, or fail closed.

        A blank or relative value must never reach the payment provider, and https is
        required outside localhost.
        """
        raw = (os.getenv(env_name) or "").strip()
        if not raw:
            raise CustomException(code=503, name=McpErrorMessages.MCP_CARD_CONFIG_INVALID,
                                  details=f"{env_name} is not configured")
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise CustomException(code=503, name=McpErrorMessages.MCP_CARD_CONFIG_INVALID,
                                  details=f"{env_name} must be an absolute http(s) URL")
        if parsed.scheme != "https" and (parsed.hostname or "").lower() not in _LOCAL_HOSTS:
            raise CustomException(code=503, name=McpErrorMessages.MCP_CARD_CONFIG_INVALID,
                                  details=f"{env_name} must use HTTPS outside localhost")
        return raw

    @staticmethod
    def __session_expires_at() -> int:
        """Absolute expiry as the unix timestamp the provider expects, clamped to its window."""
        try:
            minutes = int(os.getenv(MCP_CARD_SESSION_EXPIRY_MINUTES_ENV, DEFAULT_CARD_SESSION_EXPIRY_MINUTES))
        except (TypeError, ValueError):
            minutes = DEFAULT_CARD_SESSION_EXPIRY_MINUTES
        minutes = max(MIN_CARD_SESSION_EXPIRY_MINUTES, min(MAX_CARD_SESSION_EXPIRY_MINUTES, minutes))
        return int((datetime.now(tz=dt_timezone.utc) + timedelta(minutes=minutes)).timestamp())

    @staticmethod
    def __isoformat(unix_timestamp) -> Optional[str]:
        if not unix_timestamp:
            return None
        try:
            return datetime.fromtimestamp(int(unix_timestamp), tz=dt_timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None
