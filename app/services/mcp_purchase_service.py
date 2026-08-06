"""The MCP wallet adapter: translate, delegate, report.

This service performs **no** payment, wallet, order or provisioning work of its own. It
validates the MCP request, translates it into the same ``AssignRequest`` a mobile or web
client sends, and calls :meth:`app.services.user_service.UserBundleService.assign` -- the
one entry point the website and the mobile application already use. Bundle lookup, bundle
availability, pricing, the wallet balance check, the wallet debit, order creation and eSIM
provisioning all happen inside that call, exactly as they do for legacy traffic.

What this service adds on top is honesty about the outcome. The shared flow answers
``COMPLETED`` as soon as its wallet branch finishes, but its provisioning helper reports a
failed eSIM hub order by *returning* an exception object rather than raising one, so a
wallet purchase whose provisioning failed still answers ``COMPLETED``. That is long-
standing behaviour the website depends on and it is left exactly as it is. For MCP, the
order row the flow just wrote is read back through the existing repository and the answer
is derived from it, so "the wallet was charged and no eSIM exists" surfaces as an
escalation instead of a success.
"""

from typing import Optional

from fastapi import Request
from loguru import logger

from app.config.db import PaymentTypeEnum
from app.config.mcp_constants import McpErrorMessages, McpNextAction, McpPurchaseStatus
from app.exceptions import CustomException
from app.models.user import UserModel, UserOrderModel
from app.repo import UserOrderRepo, UserProfileRepo
from app.schemas.bundle import AssignRequest
from app.schemas.mcp import McpAssignRequest, McpAssignResponse
from app.schemas.response import Response, ResponseHelper
from app.services.mcp_common import (
    ensure_authenticated_user,
    ensure_mcp_purchase_enabled,
    ensure_supported_currency,
    order_did_not_succeed,
    order_is_cancelled,
    order_is_failed,
    order_is_paid,
    order_succeeded,
    to_related_search,
)
from app.services.user_service import UserBundleService


class McpPurchaseService:
    """Adapter in front of the existing wallet purchase flow. Owns no business logic."""

    def __init__(
        self,
        user_bundle_service: Optional[UserBundleService] = None,
        user_order_repo: Optional[UserOrderRepo] = None,
        user_profile_repo: Optional[UserProfileRepo] = None,
    ):
        # Dependencies are injectable so a test can exercise this adapter without
        # constructing the eSIM hub client or reaching a database. Production callers
        # pass nothing and get the same instances every other service builds.
        self.__user_bundle_service = user_bundle_service if user_bundle_service is not None else UserBundleService()
        self.__user_order_repo = user_order_repo if user_order_repo is not None else UserOrderRepo()
        self.__user_profile_repo = user_profile_repo if user_profile_repo is not None else UserProfileRepo()

    async def assign_wallet_bundle(
        self,
        user: UserModel,
        device_id: str,
        mcp_request: McpAssignRequest,
        x_currency: str,
        locale: str,
        request: Request,
    ) -> Response[McpAssignResponse]:
        """Buy one bundle from the caller's wallet through the existing flow.

        ``user`` is the model the verified bearer token produced. No identifier from the
        MCP payload is used to decide whose wallet is debited, and there is no field in
        :class:`~app.schemas.mcp.McpAssignRequest` that could carry one.
        """
        ensure_mcp_purchase_enabled()
        user = ensure_authenticated_user(user)
        currency = ensure_supported_currency(x_currency)

        # Translated into the *legacy* request shape: the shared flow receives exactly
        # what a mobile or web client gives it. Promotions and affiliate codes are not
        # part of the MCP surface, so both are explicitly absent rather than defaulted.
        assign_request = AssignRequest(
            bundle_code=mcp_request.bundle_code,
            related_search=to_related_search(mcp_request.related_search),
            promo_code=None,
            affiliate_code=None,
            payment_type=PaymentTypeEnum.WALLET,
        )

        logger.info(f"mcp wallet purchase delegating to the shared assign flow for bundle {mcp_request.bundle_code}")

        # Everything that matters happens in here: bundle availability, server-side
        # pricing, the wallet balance check, the debit, order creation and provisioning.
        # An insufficient balance raises the existing CustomException and propagates
        # unchanged, so the MCP client sees the platform's own INSUFFICIENT_WALLET_BALANCE.
        legacy_response = await self.__user_bundle_service.assign(
            user=user,
            device_id=device_id,
            assign_request=assign_request,
            x_currency=currency,
            locale=locale,
            request=request,
        )

        order_id = getattr(getattr(legacy_response, "data", None), "order_id", None)
        if not order_id:
            # The shared flow always returns an order id on success. Without one there is
            # nothing to confirm and nothing to poll, and claiming success would be a
            # guess about money.
            raise CustomException(
                code=502,
                name=McpErrorMessages.MCP_DEPENDENCY_UNAVAILABLE,
                details="The purchase flow did not return an order reference",
            )

        return self.__describe_outcome(user=user, order_id=order_id, quote_reference=mcp_request.quote_reference)

    def __describe_outcome(self, user: UserModel, order_id: str, quote_reference: Optional[str]) -> Response:
        """Derive the reported outcome from the order row the shared flow just wrote.

        Read-only, through the existing repositories, scoped to the authenticated user.
        Nothing here mutates an order, a wallet or a profile.
        """
        order: Optional[UserOrderModel] = self.__user_order_repo.get_first_by({"id": order_id, "user_id": user.id})
        provisioned = (
            self.__user_profile_repo.get_first_by({"user_order_id": order_id, "user_id": user.id}) is not None
        )

        payment_status = getattr(order, "payment_status", None)
        order_status = getattr(order, "order_status", None)

        if order is None:
            # The row exists (the flow created it) but is not readable as this user's.
            # Never reported as a failure: money may have moved.
            return self.__answer(
                status=McpPurchaseStatus.MANUAL_INTERVENTION_REQUIRED,
                http_status=424,
                order_id=order_id,
                payment_status=None,
                order_status=None,
                provisioned=False,
                quote_reference=quote_reference,
                next_action=McpNextAction.CONTACT_SUPPORT,
                message="The purchase outcome could not be confirmed. Do not retry; contact support.",
            )

        paid = order_is_paid(payment_status)

        if paid and order_succeeded(order_status) and provisioned:
            return self.__answer(
                status=McpPurchaseStatus.COMPLETED,
                http_status=200,
                order_id=order_id,
                payment_status=payment_status,
                order_status=order_status,
                provisioned=True,
                quote_reference=quote_reference,
                next_action=McpNextAction.GET_ESIM_BY_ORDER,
                message="The order was created and the wallet was charged.",
            )

        if paid and (order_did_not_succeed(order_status) or not provisioned):
            # The wallet was debited but no eSIM exists. This is the one outcome that
            # must never be retried and never reported as either success or failure.
            logger.error(f"mcp wallet purchase charged without provisioning for order {order_id}")
            return self.__answer(
                status=McpPurchaseStatus.MANUAL_INTERVENTION_REQUIRED,
                http_status=424,
                order_id=order_id,
                payment_status=payment_status,
                order_status=order_status,
                provisioned=provisioned,
                quote_reference=quote_reference,
                next_action=McpNextAction.CONTACT_SUPPORT,
                message="The wallet was charged but the eSIM was not issued. Do not retry; contact support.",
            )

        if order_is_failed(payment_status) or order_is_cancelled(order_status) or order_did_not_succeed(order_status):
            return self.__answer(
                status=McpPurchaseStatus.FAILED,
                http_status=200,
                order_id=order_id,
                payment_status=payment_status,
                order_status=order_status,
                provisioned=provisioned,
                quote_reference=quote_reference,
                next_action=None,
                message="The purchase did not complete. Nothing was charged.",
            )

        return self.__answer(
            status=McpPurchaseStatus.PENDING,
            http_status=200,
            order_id=order_id,
            payment_status=payment_status,
            order_status=order_status,
            provisioned=provisioned,
            quote_reference=quote_reference,
            next_action=None,
            message="The purchase has not settled yet.",
        )

    @staticmethod
    def __answer(
        status: McpPurchaseStatus,
        http_status: int,
        order_id: Optional[str],
        payment_status: Optional[str],
        order_status: Optional[str],
        provisioned: bool,
        quote_reference: Optional[str],
        next_action: Optional[McpNextAction],
        message: str,
    ) -> Response:
        payload = McpAssignResponse(
            status=status,
            order_id=order_id,
            payment_method=PaymentTypeEnum.WALLET.value,
            payment_status=str(payment_status) if payment_status is not None else None,
            order_status=str(order_status) if order_status is not None else None,
            provisioning_status=McpPurchaseStatus.COMPLETED.value if provisioned else None,
            next_action=next_action,
            quote_reference=quote_reference,
            idempotent_replay=False,
            message=message,
        )
        if http_status == 200:
            return ResponseHelper.success_data_response(payload, 0)
        # A non-2xx outcome still carries the payload, because the MCP client needs the
        # order id to escalate with. The envelope reports the failure.
        response = ResponseHelper.success_data_response(payload, 0)
        response.status = "failed"
        response.responseCode = http_status
        response.title = McpErrorMessages.MCP_MANUAL_INTERVENTION_REQUIRED.value
        response.message = message
        return response
