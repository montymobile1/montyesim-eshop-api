"""Isolated, feature flagged, database-backed idempotent MCP wallet purchase.

Design notes
------------
* The legacy ``POST /api/v1/user/bundle/assign`` flow is untouched. This service
  re-uses ``UserBundleService.assign`` by passing an *optional* execution context
  which legacy callers never pass, so legacy behaviour is unchanged.
* Nothing about price, tax, wallet balance, user identity, order id or payment
  status is taken from the caller: the user comes from the Bearer token, the
  bundle/price from the eSIM Hub, the balance from the wallet service.
* Concurrency is resolved by PostgreSQL through an atomic claim RPC, never by a
  process local lock.
* The raw ``Idempotency-Key`` is validated, hashed, and then dropped. Only the
  digest and a short fingerprint ever leave ``mcp_idempotency``.
"""

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, NamedTuple, Optional

from fastapi import Request
from loguru import logger

from app.config.config import esim_hub_service_instance
from app.config.constants import ErrorMessages
from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.config.feature_flags import is_mcp_purchase_enabled
from app.config.mcp_constants import (
    DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    DEFAULT_PROCESSING_TIMEOUT_SECONDS,
    HTTP_MANUAL_INTERVENTION,
    MCP_WALLET_BUNDLE_ASSIGN,
    McpClaimOutcome,
    McpErrorMessages,
    McpIdempotencyStatus,
    McpNextAction,
    McpOrderStatus,
    McpPaymentStatus,
    McpProvisioningStatus,
    McpPurchaseStatus,
)
from app.exceptions import CustomException
from app.i18n import translate
from app.models.mcp import McpIdempotencyClaim, McpPurchaseIdempotencyModel
from app.models.user import UserModel, UserOrderModel, UserWalletModel
from app.repo import UserOrderRepo, UserProfileRepo
from app.repo.mcp_idempotency_repo import McpPurchaseIdempotencyRepo
from app.schemas.bundle import AssignRequest, CountryRequestDto, RegionRequestDto, RelatedSearchRequestDto
from app.schemas.mcp import McpAssignRequest, McpPurchaseResponse
from app.schemas.response import Response, ResponseHelper
from app.services.currency_service import CurrencyService
from app.services.mcp_idempotency import (
    build_request_hash,
    fingerprint,
    hash_idempotency_key,
    user_fingerprint,
    validate_idempotency_key,
)
from app.services.user_service import UserBundleService
from app.services.user_wallet_service import UserWalletService

#: Business failures that are deterministic: replaying the same key replays the failure.
_FINAL_BUSINESS_ERRORS = {
    str(ErrorMessages.BUNDLE_NOT_AVAILABLE),
    str(ErrorMessages.INSUFFICIENT_WALLET_BALANCE),
    str(ErrorMessages.WALLET_NOT_FOUND),
    str(McpErrorMessages.MCP_UNSUPPORTED_PAYMENT_TYPE),
}


class McpPurchaseResult(NamedTuple):
    """What the router should send back."""

    envelope: Response[McpPurchaseResponse]
    http_status: int
    idempotent_replay: bool


class McpWalletPurchaseContext:
    """Observer handed to the shared purchase flow.

    It records *what already happened* so the MCP endpoint can distinguish
    "nothing was charged" from "charged, provisioning unclear". It never raises,
    never mutates the order and never changes control flow.
    """

    def __init__(self, idempotency_repo: McpPurchaseIdempotencyRepo, record_id: str, user_id: str,
                 correlation_id: str):
        self.__repo = idempotency_repo
        self.__record_id = record_id
        self.__user_id = user_id
        self.correlation_id = correlation_id
        self.order_id: Optional[str] = None
        self.order_persisted: bool = False
        self.wallet_debited: bool = False
        self.provisioning_called: bool = False
        self.provisioning_failed: bool = False

    def on_order_created(self, order_id: str) -> None:
        self.order_id = order_id
        try:
            self.__repo.attach_order(record_id=self.__record_id, user_id=self.__user_id, order_id=order_id)
            self.order_persisted = True
        except Exception as e:  # never break the purchase because of a correlation write
            logger.error(f"mcp.purchase correlation_id={self.correlation_id} "
                         f"failed to persist order correlation: {e}")
        logger.info(f"mcp.purchase correlation_id={self.correlation_id} state=ORDER_CREATED "
                    f"order_id={order_id} correlation_persisted={self.order_persisted}")

    def on_wallet_debited(self, amount: float, currency: str) -> None:
        self.wallet_debited = True
        # Amount/balance intentionally not logged: the currency is enough for triage.
        logger.info(f"mcp.purchase correlation_id={self.correlation_id} state=WALLET_DEBITED "
                    f"order_id={self.order_id} currency={currency}")

    def on_provisioning_result(self, result: Any) -> None:
        self.provisioning_called = True
        self.provisioning_failed = result is None or isinstance(result, Exception)
        logger.info(f"mcp.purchase correlation_id={self.correlation_id} state=PROVISIONING_RETURNED "
                    f"order_id={self.order_id} failed={self.provisioning_failed}")


class McpPurchaseService:

    def __init__(self):
        self.__idempotency_repo = McpPurchaseIdempotencyRepo()
        self.__user_bundle_service = UserBundleService()
        self.__user_wallet_service = UserWalletService()
        self.__currency_service = CurrencyService()
        self.__esim_hub_service = esim_hub_service_instance()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()

    # ------------------------------------------------------------------ public

    async def assign_wallet_bundle(self, user: UserModel, device_id: str, mcp_request: McpAssignRequest,
                                   idempotency_key: Optional[str], x_currency: str, locale: str,
                                   request: Request) -> McpPurchaseResult:
        correlation_id = uuid.uuid4().hex
        self.__ensure_enabled()
        self.__ensure_authenticated_user(user)

        raw_key = validate_idempotency_key(idempotency_key)
        if mcp_request.payment_type != PaymentTypeEnum.WALLET:
            raise CustomException(code=400, name=McpErrorMessages.MCP_UNSUPPORTED_PAYMENT_TYPE,
                                  details="Only Wallet payments are supported by the MCP purchase endpoint")

        key_hash = hash_idempotency_key(raw_key=raw_key, user_id=user.id, operation=MCP_WALLET_BUNDLE_ASSIGN)
        request_hash = build_request_hash(user_id=user.id, operation=MCP_WALLET_BUNDLE_ASSIGN,
                                          bundle_code=mcp_request.bundle_code,
                                          payment_type=mcp_request.payment_type,
                                          related_search=mcp_request.related_search)
        del raw_key  # the raw key must not survive past this point

        key_fp = fingerprint(key_hash)
        user_fp = user_fingerprint(user.id)
        logger.info(f"mcp.purchase correlation_id={correlation_id} state=RECEIVED user={user_fp} "
                    f"key_fp={key_fp} request_fp={fingerprint(request_hash)}")

        claim = self.__idempotency_repo.claim(user_id=user.id, operation=MCP_WALLET_BUNDLE_ASSIGN,
                                              idempotency_key_hash=key_hash, request_hash=request_hash,
                                              ttl_seconds=self.__ttl_seconds())
        logger.info(f"mcp.purchase correlation_id={correlation_id} state=CLAIM outcome={claim.outcome} "
                    f"user={user_fp} key_fp={key_fp}")

        if claim.outcome == McpClaimOutcome.CLAIMED:
            return await self.__execute(user=user, device_id=device_id, mcp_request=mcp_request, claim=claim,
                                        x_currency=x_currency, locale=locale, request=request,
                                        correlation_id=correlation_id, key_fp=key_fp)

        if claim.outcome == McpClaimOutcome.CONFLICT:
            raise CustomException(
                code=409, name=McpErrorMessages.IDEMPOTENCY_KEY_CONFLICT,
                details=("This Idempotency-Key was already used for a different purchase request. "
                         "Use a new Idempotency-Key for a different purchase."))

        if claim.outcome in (McpClaimOutcome.PROCESSING, McpClaimOutcome.RETRY):
            return self.__handle_in_progress(user=user, claim=claim, mcp_request=mcp_request,
                                             correlation_id=correlation_id)

        # SUCCEEDED / FAILED_FINAL / FAILED_RETRYABLE / AMBIGUOUS -> durable replay
        return self.__replay(claim=claim, mcp_request=mcp_request, correlation_id=correlation_id)

    # ------------------------------------------------------------- guard rails

    @staticmethod
    def __ensure_enabled():
        if not is_mcp_purchase_enabled():
            raise CustomException(code=503, name=McpErrorMessages.MCP_PURCHASE_DISABLED,
                                  details="MCP purchase is not enabled in this environment")

    @staticmethod
    def __ensure_authenticated_user(user: UserModel | None):
        if user is None or not getattr(user, "id", None):
            raise CustomException(code=401, name=ErrorMessages.BEARER_TOKEN_REQUIRED,
                                  details="An authenticated user is required for this operation")
        if getattr(user, "is_anonymous", False):
            raise CustomException(code=401, name=McpErrorMessages.MCP_ANONYMOUS_NOT_ALLOWED,
                                  details="Anonymous sessions cannot use the MCP purchase endpoint")

    @staticmethod
    def __ttl_seconds() -> int:
        try:
            return int(os.getenv("MCP_IDEMPOTENCY_TTL_SECONDS", DEFAULT_IDEMPOTENCY_TTL_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_IDEMPOTENCY_TTL_SECONDS

    @staticmethod
    def __processing_timeout_seconds() -> int:
        try:
            return int(os.getenv("MCP_IDEMPOTENCY_PROCESSING_TIMEOUT_SECONDS",
                                 DEFAULT_PROCESSING_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_PROCESSING_TIMEOUT_SECONDS

    # ---------------------------------------------------------------- execution

    async def __execute(self, user: UserModel, device_id: str, mcp_request: McpAssignRequest,
                        claim: McpIdempotencyClaim, x_currency: str, locale: str, request: Request,
                        correlation_id: str, key_fp: str) -> McpPurchaseResult:
        record_id = claim.record_id
        context = McpWalletPurchaseContext(idempotency_repo=self.__idempotency_repo, record_id=record_id,
                                           user_id=user.id, correlation_id=correlation_id)

        # ---- authoritative pre-flight: bundle, price and wallet are re-fetched here
        try:
            await self.__preflight(user=user, mcp_request=mcp_request, correlation_id=correlation_id)
        except CustomException as ce:
            final = str(ce.name) in _FINAL_BUSINESS_ERRORS
            status = McpIdempotencyStatus.FAILED_FINAL if final else McpIdempotencyStatus.FAILED_RETRYABLE
            return self.__finish_failure(record_id=record_id, user=user, context=context, status=status,
                                         http_status=ce.code, error_code=str(ce.name),
                                         mcp_request=mcp_request, correlation_id=correlation_id,
                                         message=str(ce.details))
        except Exception as e:
            logger.error(f"mcp.purchase correlation_id={correlation_id} state=PREFLIGHT_ERROR key_fp={key_fp}: {e}")
            return self.__finish_failure(record_id=record_id, user=user, context=context,
                                         status=McpIdempotencyStatus.FAILED_RETRYABLE, http_status=503,
                                         error_code=str(McpErrorMessages.MCP_PURCHASE_TEMPORARILY_UNAVAILABLE),
                                         mcp_request=mcp_request, correlation_id=correlation_id,
                                         message="Purchase could not be validated, retry with the same key")

        # ---- execute the shared purchase flow exactly once
        legacy_request = self.__to_legacy_assign_request(mcp_request)
        try:
            await self.__user_bundle_service.assign(user=user, device_id=device_id,
                                                    assign_request=legacy_request, x_currency=x_currency,
                                                    locale=locale, request=request,
                                                    execution_context=context)
            execution_error = None
        except CustomException as ce:
            execution_error = ce
            logger.error(f"mcp.purchase correlation_id={correlation_id} state=EXECUTION_FAILED "
                         f"error_code={ce.name} order_id={context.order_id}")
        except Exception as e:
            execution_error = CustomException(code=500, name=ErrorMessages.REQUEST_FAILED, details=str(e))
            logger.error(f"mcp.purchase correlation_id={correlation_id} state=EXECUTION_EXCEPTION "
                         f"order_id={context.order_id}: {e}")

        return self.__finalize(user=user, record_id=record_id, context=context, mcp_request=mcp_request,
                               execution_error=execution_error, correlation_id=correlation_id)

    async def __preflight(self, user: UserModel, mcp_request: McpAssignRequest, correlation_id: str):
        """Re-validate everything that matters before any financial side effect.

        Price and availability come from the eSIM Hub, the balance from the wallet
        service, and every comparison is done with ``Decimal``.
        """
        bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id=mcp_request.bundle_code)
        if not bundle or not bundle.is_active:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                  details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        if not bundle.is_stockable:
            applicable = await self.__esim_hub_service.check_bundle_applicable(bundle.bundle_info_code)
            if not applicable:
                raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                      details=ErrorMessages.BUNDLE_NOT_AVAILABLE)

        wallet: UserWalletModel = self.__user_wallet_service.get_user_wallet(user_id=user.id)
        if wallet is None:
            raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")

        required = self.__required_amount(price_usd=bundle.original_price, wallet_currency=wallet.currency)
        balance = self.__to_decimal(wallet.amount)
        logger.info(f"mcp.purchase correlation_id={correlation_id} state=PREFLIGHT_OK "
                    f"currency={wallet.currency} sufficient={balance >= required}")
        if balance < required:
            raise CustomException(code=400, name=ErrorMessages.INSUFFICIENT_WALLET_BALANCE,
                                  details="Insufficient wallet balance, please top up your wallet")

    def __required_amount(self, price_usd: Any, wallet_currency: str) -> Decimal:
        """Authoritative price in the wallet currency, computed with Decimal only."""
        rate = self.__to_decimal(self.__currency_service.get_currency_rate(from_currency="USD",
                                                                          to_currency=wallet_currency))
        price = self.__to_decimal(price_usd)
        return (price * rate).quantize(Decimal("0.00"), rounding=ROUND_DOWN)

    @staticmethod
    def __to_decimal(value: Any) -> Decimal:
        """Convert provider/database values through ``str`` so no binary float error leaks in."""
        if value is None:
            return Decimal("0")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @staticmethod
    def __to_legacy_assign_request(mcp_request: McpAssignRequest) -> AssignRequest:
        """Normalize the MCP body into the internal request the shared flow expects."""
        related = mcp_request.related_search
        region = None
        countries = None
        if related is not None:
            if related.region is not None:
                region = RegionRequestDto(iso_code=related.region.iso_code,
                                          region_name=related.region.region_name)
            if related.countries is not None:
                countries = [CountryRequestDto(iso3_code=country.iso3_code, country_name=country.country_name)
                             for country in related.countries]
        return AssignRequest(bundle_code=mcp_request.bundle_code,
                             related_search=RelatedSearchRequestDto(region=region, countries=countries),
                             promo_code=None, affiliate_code=None,
                             payment_type=PaymentTypeEnum.WALLET)

    # --------------------------------------------------------------- finalizing

    def __finalize(self, user: UserModel, record_id: str, context: McpWalletPurchaseContext,
                   mcp_request: McpAssignRequest, execution_error: Optional[CustomException],
                   correlation_id: str) -> McpPurchaseResult:
        """Decide the terminal state from the authoritative order/profile state."""
        order, profile_exists, inspection_failed = self.__inspect_order(user_id=user.id,
                                                                        order_id=context.order_id)
        provisioned = (order is not None
                       and str(order.payment_status) == str(OrderStatusEnum.SUCCESS)
                       and str(order.order_status) == str(OrderStatusEnum.SUCCESS)
                       and profile_exists
                       and not context.provisioning_failed)

        if provisioned and execution_error is None:
            response = McpPurchaseResponse(
                status=McpPurchaseStatus.COMPLETED, order_id=context.order_id,
                payment_status=McpPaymentStatus.COMPLETED, order_status=McpOrderStatus.SUCCESS,
                idempotent_replay=False, provisioning_status=McpProvisioningStatus.COMPLETED,
                next_action=McpNextAction.GET_ESIM_BY_ORDER,
                quote_reference=mcp_request.quote_reference, correlation_id=correlation_id)
            self.__persist_terminal(record_id=record_id, user=user, status=McpIdempotencyStatus.SUCCEEDED,
                                    http_status=200, response=response, error_code=None,
                                    order_id=context.order_id, correlation_id=correlation_id)
            return McpPurchaseResult(envelope=ResponseHelper.success_data_response(response, 0),
                                     http_status=200, idempotent_replay=False)

        # Money may have moved without a usable eSIM: never claim success, never auto-refund,
        # never retry automatically.
        charged = context.wallet_debited or (order is not None
                                             and str(order.payment_status) == str(OrderStatusEnum.SUCCESS))
        if charged or inspection_failed:
            response = McpPurchaseResponse(
                status=McpPurchaseStatus.MANUAL_INTERVENTION_REQUIRED, order_id=context.order_id,
                payment_status=McpPaymentStatus.UNKNOWN_OR_SUCCESS,
                order_status=McpOrderStatus.FAILURE_OR_UNKNOWN, idempotent_replay=False,
                provisioning_status=McpProvisioningStatus.UNKNOWN,
                next_action=McpNextAction.CONTACT_SUPPORT,
                quote_reference=mcp_request.quote_reference, correlation_id=correlation_id,
                message="Wallet may have been charged without a completed eSIM. Do not retry this purchase.")
            self.__persist_terminal(record_id=record_id, user=user, status=McpIdempotencyStatus.AMBIGUOUS,
                                    http_status=HTTP_MANUAL_INTERVENTION, response=response,
                                    error_code=str(McpErrorMessages.MCP_MANUAL_INTERVENTION_REQUIRED),
                                    order_id=context.order_id, correlation_id=correlation_id)
            return McpPurchaseResult(
                envelope=self.__failure_envelope(response, HTTP_MANUAL_INTERVENTION,
                                                 str(McpErrorMessages.MCP_MANUAL_INTERVENTION_REQUIRED)),
                http_status=HTTP_MANUAL_INTERVENTION, idempotent_replay=False)

        error_code = str(execution_error.name) if execution_error is not None else str(ErrorMessages.ORDER_FAILED)
        http_status = execution_error.code if execution_error is not None else 400
        return self.__finish_failure(record_id=record_id, user=user, context=context,
                                     status=McpIdempotencyStatus.FAILED_FINAL, http_status=http_status,
                                     error_code=error_code, mcp_request=mcp_request,
                                     correlation_id=correlation_id,
                                     message="Purchase did not complete, nothing was charged")

    def __finish_failure(self, record_id: str, user: UserModel, context: McpWalletPurchaseContext,
                         status: str, http_status: int, error_code: str, mcp_request: McpAssignRequest,
                         correlation_id: str, message: str) -> McpPurchaseResult:
        next_action = (McpNextAction.RETRY_SAME_IDEMPOTENCY_KEY
                       if status == McpIdempotencyStatus.FAILED_RETRYABLE
                       else McpNextAction.RETRY_WITH_NEW_IDEMPOTENCY_KEY)
        response = McpPurchaseResponse(
            status=McpPurchaseStatus.FAILED, order_id=context.order_id,
            payment_status=McpPaymentStatus.NOT_CHARGED,
            order_status=McpOrderStatus.FAILURE if context.order_id else McpOrderStatus.NOT_CREATED,
            idempotent_replay=False, provisioning_status=McpProvisioningStatus.NOT_STARTED,
            next_action=next_action, quote_reference=mcp_request.quote_reference,
            correlation_id=correlation_id, message=message)
        self.__persist_terminal(record_id=record_id, user=user, status=status, http_status=http_status,
                                response=response, error_code=error_code, order_id=context.order_id,
                                correlation_id=correlation_id)
        return McpPurchaseResult(envelope=self.__failure_envelope(response, http_status, error_code),
                                 http_status=http_status, idempotent_replay=False)

    def __inspect_order(self, user_id: str, order_id: Optional[str]):
        """Return ``(order, profile_exists, inspection_failed)`` scoped to the owner."""
        if not order_id:
            return None, False, False
        try:
            order: UserOrderModel = self.__user_order_repo.get_first_by({"id": order_id, "user_id": user_id})
            if order is None:
                return None, False, False
            profile = self.__user_profile_repo.get_first_by({"user_id": user_id, "user_order_id": order_id})
            return order, profile is not None, False
        except Exception as e:
            logger.error(f"mcp.purchase failed to inspect order {order_id}: {e}")
            return None, False, True

    def __persist_terminal(self, record_id: str, user: UserModel, status: str, http_status: int,
                           response: McpPurchaseResponse, error_code: Optional[str],
                           order_id: Optional[str], correlation_id: str):
        body = response.model_dump(mode="json")
        # Stored bodies are replayed verbatim; per-request values are re-stamped on replay.
        body["idempotent_replay"] = False
        body["correlation_id"] = None
        try:
            self.__idempotency_repo.mark_terminal(record_id=record_id, user_id=user.id, status=status,
                                                  response_code=http_status, response_body=body,
                                                  error_code=error_code, order_id=order_id)
        except Exception as e:
            logger.error(f"mcp.purchase correlation_id={correlation_id} failed to persist terminal state "
                         f"{status}: {e}")
        logger.info(f"mcp.purchase correlation_id={correlation_id} state={status} "
                    f"http={http_status} order_id={order_id} error_code={error_code}")

    # ------------------------------------------------------------------ replay

    def __replay(self, claim: McpIdempotencyClaim, mcp_request: McpAssignRequest,
                 correlation_id: str) -> McpPurchaseResult:
        body = claim.response_body or {}
        http_status = claim.response_code or 200
        if not body:
            # A terminal record without a stored body cannot be replayed safely.
            return self.__ambiguous_replay(order_id=claim.order_id, mcp_request=mcp_request,
                                           correlation_id=correlation_id)
        try:
            response = McpPurchaseResponse(**body)
        except Exception as e:
            logger.error(f"mcp.purchase correlation_id={correlation_id} stored response is unreadable: {e}")
            return self.__ambiguous_replay(order_id=claim.order_id, mcp_request=mcp_request,
                                           correlation_id=correlation_id)

        response.idempotent_replay = True
        response.correlation_id = correlation_id
        logger.info(f"mcp.purchase correlation_id={correlation_id} state=REPLAY stored_status={claim.status} "
                    f"http={http_status} order_id={claim.order_id}")
        if http_status == 200 and response.status == McpPurchaseStatus.COMPLETED:
            envelope = ResponseHelper.success_data_response(response, 0)
        else:
            envelope = self.__failure_envelope(response, http_status, claim.error_code)
        return McpPurchaseResult(envelope=envelope, http_status=http_status, idempotent_replay=True)

    def __handle_in_progress(self, user: UserModel, claim: McpIdempotencyClaim, mcp_request: McpAssignRequest,
                             correlation_id: str) -> McpPurchaseResult:
        """A concurrent or crashed execution owns this key: never start a second purchase."""
        record: Optional[McpPurchaseIdempotencyModel] = None
        if claim.record_id:
            try:
                record = self.__idempotency_repo.get_for_user(record_id=claim.record_id, user_id=user.id)
            except Exception as e:
                logger.error(f"mcp.purchase correlation_id={correlation_id} could not read record: {e}")

        stale = self.__is_stale(record.updated_at if record else claim.updated_at)
        if not stale:
            raise CustomException(
                code=409, name=McpErrorMessages.IDEMPOTENT_REQUEST_IN_PROGRESS,
                details=(f"The purchase for this Idempotency-Key is still processing. Retry the very same "
                         f"Idempotency-Key after {self.__processing_timeout_seconds()} seconds. "
                         f"Do not create a new key and do not start a second purchase."))

        order_id = (record.order_id if record else None) or claim.order_id
        order, profile_exists, inspection_failed = self.__inspect_order(user_id=user.id, order_id=order_id)
        record_id = record.id if record else claim.record_id

        if (order is not None and not inspection_failed
                and str(order.payment_status) == str(OrderStatusEnum.SUCCESS)
                and str(order.order_status) == str(OrderStatusEnum.SUCCESS) and profile_exists):
            response = McpPurchaseResponse(
                status=McpPurchaseStatus.COMPLETED, order_id=order_id,
                payment_status=McpPaymentStatus.COMPLETED, order_status=McpOrderStatus.SUCCESS,
                idempotent_replay=True, provisioning_status=McpProvisioningStatus.COMPLETED,
                next_action=McpNextAction.GET_ESIM_BY_ORDER, quote_reference=mcp_request.quote_reference,
                correlation_id=correlation_id, message="Recovered from an interrupted execution")
            if record_id:
                self.__persist_terminal(record_id=record_id, user=user,
                                        status=McpIdempotencyStatus.SUCCEEDED, http_status=200,
                                        response=response, error_code=None, order_id=order_id,
                                        correlation_id=correlation_id)
            return McpPurchaseResult(envelope=ResponseHelper.success_data_response(response, 0),
                                     http_status=200, idempotent_replay=True)

        # Cannot prove what happened: report ambiguity, never re-execute.
        return self.__ambiguous_replay(order_id=order_id, mcp_request=mcp_request,
                                       correlation_id=correlation_id, user=user, record_id=record_id)

    def __ambiguous_replay(self, order_id: Optional[str], mcp_request: McpAssignRequest, correlation_id: str,
                           user: Optional[UserModel] = None,
                           record_id: Optional[str] = None) -> McpPurchaseResult:
        response = McpPurchaseResponse(
            status=McpPurchaseStatus.MANUAL_INTERVENTION_REQUIRED, order_id=order_id,
            payment_status=McpPaymentStatus.UNKNOWN_OR_SUCCESS,
            order_status=McpOrderStatus.FAILURE_OR_UNKNOWN, idempotent_replay=True,
            provisioning_status=McpProvisioningStatus.UNKNOWN, next_action=McpNextAction.CONTACT_SUPPORT,
            quote_reference=mcp_request.quote_reference, correlation_id=correlation_id,
            message="Previous execution state could not be resolved. Do not retry this purchase.")
        if user is not None and record_id:
            self.__persist_terminal(record_id=record_id, user=user, status=McpIdempotencyStatus.AMBIGUOUS,
                                    http_status=HTTP_MANUAL_INTERVENTION, response=response,
                                    error_code=str(McpErrorMessages.MCP_MANUAL_INTERVENTION_REQUIRED),
                                    order_id=order_id, correlation_id=correlation_id)
        return McpPurchaseResult(
            envelope=self.__failure_envelope(response, HTTP_MANUAL_INTERVENTION,
                                             str(McpErrorMessages.MCP_MANUAL_INTERVENTION_REQUIRED)),
            http_status=HTTP_MANUAL_INTERVENTION, idempotent_replay=True)

    def __is_stale(self, updated_at: Optional[str]) -> bool:
        """True when a PROCESSING record is older than the crash detection window."""
        if not updated_at:
            return False
        try:
            value = str(updated_at).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        age = (datetime.now(tz=timezone.utc) - parsed).total_seconds()
        return age >= self.__processing_timeout_seconds()

    # ---------------------------------------------------------------- envelopes

    @staticmethod
    def __failure_envelope(response: McpPurchaseResponse, http_status: int,
                           error_code: Optional[str]) -> Response[McpPurchaseResponse]:
        """Failure carried inside the existing envelope, with the MCP payload attached."""
        title = translate(error_code) if error_code else None
        return Response(status="failed", totalCount=0, data=response, title=title,
                        message=response.message or title, developerMessage=error_code,
                        responseCode=http_status)
