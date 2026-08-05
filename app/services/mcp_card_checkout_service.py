"""MCP-only Stripe hosted checkout (Phase 5A).

Design notes
------------
* Nothing here touches the legacy Card flow. The legacy flow builds a PaymentIntent
  for a native SDK sheet; this builds a *hosted* Checkout Session and returns a URL.
  Card data therefore never reaches the chatbot, the MCP server or this backend.
* Price, currency, user and bundle are always re-derived server side. The request body
  carries quote identity only, and forbids extra fields, so an amount cannot be
  injected even by a malicious caller.
* Idempotency reuses the existing ``mcp_purchase_idempotency`` table under a *different
  operation*, so a wallet key and a card key can never collide.
* The same digest is used for backend persistence and for Stripe's own idempotency key,
  which is what makes a timeout safe: replaying returns Stripe's original Session.
* A browser redirect never marks anything paid. Only a signature-verified
  ``payment_intent.*`` webhook - the same event the legacy Card flow uses - can move
  money-related state, and only when it carries this feature's own metadata marker.
"""

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple, Optional

from loguru import logger

from app.config.config import esim_hub_service_instance
from app.config.constants import ErrorMessages
from app.config.db import OrderStatusEnum, PaymentTypeEnum, UserOrderType
from app.config.feature_flags import is_mcp_card_purchase_enabled
from app.config.mcp_card_constants import (
    MCP_CARD_BUNDLE_CHECKOUT,
    METADATA_SOURCE_KEY,
    METADATA_SOURCE_VALUE,
    McpCardErrorMessages,
    McpCardStatus,
)
from app.config.mcp_constants import (
    DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    McpClaimOutcome,
    McpErrorMessages,
    McpIdempotencyStatus,
)
from app.exceptions import CustomException
from app.i18n import translate
from app.models.mcp import McpIdempotencyClaim
from app.models.user import UserModel
from app.repo import UserOrderRepo
from app.repo.mcp_card_checkout_repo import McpCardCheckoutRepo
from app.repo.mcp_idempotency_repo import McpPurchaseIdempotencyRepo
from app.schemas.mcp_card import (
    STATUS_NEXT_ACTION,
    McpCardCheckoutRequest,
    McpCardCheckoutResponse,
    McpCardNextAction,
    McpCardStatusResponse,
)
from app.schemas.response import Response, ResponseHelper
from app.services.mcp_idempotency import (
    build_card_request_hash,
    effective_currency,
    fingerprint,
    hash_idempotency_key,
    require_hash_secret,
    system_currency,
    user_fingerprint,
    validate_idempotency_key,
)
from app.services.mcp_stripe_gateway import (
    McpStripeCheckoutGateway,
    StripeGatewayError,
    cancel_url,
    redact,
    session_expiry_minutes,
    success_url,
)


class McpCardResult(NamedTuple):
    envelope: Response
    http_status: int
    idempotent_replay: bool


class McpCardCheckoutService:

    def __init__(self):
        self.__idempotency_repo = McpPurchaseIdempotencyRepo()
        self.__checkout_repo = McpCardCheckoutRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__esim_hub_service = esim_hub_service_instance()
        self.__gateway = McpStripeCheckoutGateway()

    # ------------------------------------------------------------------ public

    async def create_checkout(self, user: UserModel, request: McpCardCheckoutRequest,
                              idempotency_key: Optional[str], x_currency: Optional[str],
                              device_id: Optional[str] = None) -> McpCardResult:
        correlation_id = uuid.uuid4().hex

        # Guards first: a misconfigured or unsupported request must never reach the
        # claim, so it can never write a record, an order, or a Stripe Session.
        self.__ensure_enabled()
        require_hash_secret()
        self.__ensure_card_config()
        self.__ensure_authenticated_user(user)

        raw_key = validate_idempotency_key(idempotency_key)
        currency = self.__ensure_supported_currency(x_currency)

        key_hash = hash_idempotency_key(raw_key=raw_key, user_id=user.id,
                                        operation=MCP_CARD_BUNDLE_CHECKOUT)
        request_hash = build_card_request_hash(
            user_id=user.id, operation=MCP_CARD_BUNDLE_CHECKOUT,
            bundle_code=request.bundle_code, currency=currency,
            related_search=request.related_search, quote_reference=request.quote_reference)
        del raw_key  # the raw key must not survive past this point

        key_fp = fingerprint(key_hash)
        logger.info(f"mcp.card correlation_id={correlation_id} state=RECEIVED "
                    f"user={user_fingerprint(user.id)} key_fp={key_fp} "
                    f"request_fp={fingerprint(request_hash)}")

        claim = self.__idempotency_repo.claim(
            user_id=user.id, operation=MCP_CARD_BUNDLE_CHECKOUT, idempotency_key_hash=key_hash,
            request_hash=request_hash, ttl_seconds=self.__ttl_seconds())
        logger.info(f"mcp.card correlation_id={correlation_id} state=CLAIM outcome={claim.outcome} "
                    f"key_fp={key_fp}")

        if claim.outcome == McpClaimOutcome.CLAIMED:
            return await self.__execute(user=user, request=request, claim=claim, currency=currency,
                                        key_hash=key_hash, correlation_id=correlation_id,
                                        device_id=device_id)

        if claim.outcome == McpClaimOutcome.CONFLICT:
            raise CustomException(
                code=409, name=McpErrorMessages.IDEMPOTENCY_KEY_CONFLICT,
                details=("This Idempotency-Key was already used for a different checkout request. "
                         "Use a new Idempotency-Key for a different quote or bundle."))

        if claim.outcome in (McpClaimOutcome.PROCESSING, McpClaimOutcome.RETRY):
            return self.__handle_in_progress(user=user, claim=claim, correlation_id=correlation_id)

        # An earlier attempt ended ambiguously (Stripe timed out and we cannot know
        # whether it holds a Session). Reconcile against Stripe under the SAME
        # idempotency key rather than replaying a dead 503 forever. This can never
        # produce a second Session: Stripe returns the original for a repeated key.
        reconciled = self.__reconcile_if_ambiguous(user=user, claim=claim, currency=currency,
                                                   key_hash=key_hash, request=request,
                                                   correlation_id=correlation_id,
                                                   device_id=device_id)
        if reconciled is not None:
            return reconciled

        return self.__replay(claim=claim, correlation_id=correlation_id)

    def get_status(self, user: UserModel, payment_reference: str) -> Response:
        """Owner-scoped status poll.

        A reference belonging to somebody else and a reference that never existed both
        produce the same 404, so this endpoint cannot be used to probe for the existence
        of another user's payment.
        """
        self.__ensure_enabled()
        self.__ensure_authenticated_user(user)

        reference = (payment_reference or "").strip()
        if not reference or len(reference) > 64 or not self.__is_safe_reference(reference):
            raise CustomException(code=404, name=McpCardErrorMessages.MCP_CARD_PAYMENT_NOT_FOUND,
                                  details="Payment reference not found")

        record = self.__checkout_repo.get_for_user(checkout_id=reference, user_id=user.id)
        if record is None:
            raise CustomException(code=404, name=McpCardErrorMessages.MCP_CARD_PAYMENT_NOT_FOUND,
                                  details="Payment reference not found")

        status = self.__expire_if_lapsed(record)
        response = McpCardStatusResponse(
            payment_reference=record.id, order_id=record.order_id, status=status,
            amount=self.__major_units(record.amount_minor), currency=record.currency,
            bundle_code=record.bundle_code, quote_reference=record.quote_reference,
            expires_at=record.expires_at,
            provisioned=status == McpCardStatus.COMPLETED,
            next_action=STATUS_NEXT_ACTION.get(status, McpCardNextAction.CONTACT_SUPPORT),
            message=self.__status_message(status))
        return ResponseHelper.success_data_response(response, 0)

    # ------------------------------------------------------------- guard rails

    @staticmethod
    def __ensure_enabled():
        if not is_mcp_card_purchase_enabled():
            raise CustomException(code=503, name=McpCardErrorMessages.MCP_CARD_PURCHASE_DISABLED,
                                  details="MCP card purchase is not enabled in this environment")

    @staticmethod
    def __ensure_card_config():
        """Validate redirect configuration before anything is persisted.

        Deliberately checked here rather than inside the Stripe call: a misconfigured
        deployment must fail closed *before* an order row or a checkout row exists, not
        halfway through creating one.
        """
        success_url()
        cancel_url()

    @staticmethod
    def __ensure_authenticated_user(user: Optional[UserModel]):
        if user is None or not getattr(user, "id", None):
            raise CustomException(code=401, name=ErrorMessages.BEARER_TOKEN_REQUIRED,
                                  details="An authenticated user is required for this operation")
        if getattr(user, "is_anonymous", False):
            raise CustomException(code=401, name=McpErrorMessages.MCP_ANONYMOUS_NOT_ALLOWED,
                                  details="Anonymous sessions cannot use the MCP card endpoints")

    @staticmethod
    def __ensure_supported_currency(x_currency: Optional[str]) -> str:
        supported = system_currency()
        resolved = effective_currency(x_currency)
        if resolved != supported:
            raise CustomException(
                code=400, name=McpErrorMessages.MCP_UNSUPPORTED_CURRENCY,
                details=(f"MCP card checkout is settled in {supported} only. Send X-Currency: "
                         f"{supported}, or omit the header."))
        return resolved

    def __expire_if_lapsed(self, record) -> McpCardStatus:
        """Report a lapsed PENDING checkout as EXPIRED, and persist that.

        Safety net, not the primary mechanism. Stripe cancels the PaymentIntent when a
        hosted Session expires, which reaches us as ``payment_intent.canceled`` - but
        that event is optional to subscribe to, and this feature deliberately requires
        no new webhook subscription. Deriving expiry from the stored ``expires_at``
        means a lapsed checkout is reported correctly either way.

        Only ever applied to PENDING: it is a clock-based inference, so it must never
        touch a record whose state was established by a verified payment event.
        """
        status = McpCardStatus(record.status)
        if status != McpCardStatus.PENDING or not record.expires_at:
            return status
        try:
            expires_at = datetime.fromisoformat(str(record.expires_at).replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return status
        if datetime.now(tz=timezone.utc) < expires_at:
            return status

        moved = self.__checkout_repo.transition(
            checkout_id=record.id, from_statuses=[McpCardStatus.PENDING],
            to_status=McpCardStatus.EXPIRED, extra={"failure_code": "SESSION_EXPIRED"})
        return McpCardStatus.EXPIRED if moved else McpCardStatus(record.status)

    @staticmethod
    def __is_safe_reference(reference: str) -> bool:
        return all(char.isalnum() or char in "-_" for char in reference)

    @staticmethod
    def __ttl_seconds() -> int:
        try:
            return int(os.getenv("MCP_IDEMPOTENCY_TTL_SECONDS", DEFAULT_IDEMPOTENCY_TTL_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_IDEMPOTENCY_TTL_SECONDS

    # ---------------------------------------------------------------- execution

    async def __execute(self, user: UserModel, request: McpCardCheckoutRequest,
                        claim: McpIdempotencyClaim, currency: str, key_hash: str,
                        correlation_id: str, device_id: Optional[str] = None) -> McpCardResult:
        record_id = claim.record_id

        # ---- authoritative pre-flight: the bundle and its price come from the hub
        try:
            bundle = await self.__preflight(request.bundle_code)
        except CustomException as ce:
            return self.__fail(record_id=record_id, user=user, http_status=ce.code,
                               error_code=str(ce.name), correlation_id=correlation_id,
                               status=McpIdempotencyStatus.FAILED_FINAL, message=str(ce.details),
                               currency=currency)
        except Exception as e:
            logger.error(f"mcp.card correlation_id={correlation_id} state=PREFLIGHT_ERROR: {redact(e)}")
            return self.__fail(record_id=record_id, user=user, http_status=503,
                               error_code=str(McpErrorMessages.MCP_PURCHASE_TEMPORARILY_UNAVAILABLE),
                               correlation_id=correlation_id,
                               status=McpIdempotencyStatus.FAILED_RETRYABLE,
                               message="Checkout could not be prepared, retry with the same key",
                               currency=currency)

        amount_minor = self.__authoritative_amount_minor(bundle.original_price)

        # ---- one pending order, mirroring the legacy domain shape
        order = self.__user_order_repo.create({
            "user_id": user.id,
            "bundle_id": request.bundle_code,
            "order_type": UserOrderType.ASSIGN,
            "amount": amount_minor,
            "modified_amount": amount_minor,
            "currency": currency,
            "bundle_data": bundle.model_dump_json(),
            "searched_countries": (request.related_search.model_dump_json()
                                   if request.related_search is not None else None),
            "payment_type": PaymentTypeEnum.CARD,
            # Written explicitly rather than relying on the column defaults, so the row
            # states plainly that nothing is paid or provisioned yet.
            "payment_status": OrderStatusEnum.PENDING,
            "order_status": OrderStatusEnum.PENDING,
        })
        self.__mark_side_effect(record_id=record_id, user_id=user.id, order_id=order.id,
                                correlation_id=correlation_id)

        checkout = self.__checkout_repo.create({
            "id": str(uuid.uuid4()),
            "user_id": user.id,
            "order_id": order.id,
            "bundle_code": request.bundle_code,
            "quote_reference": request.quote_reference,
            "idempotency_record_id": record_id,
            "request_hash": claim.request_hash,
            "amount_minor": amount_minor,
            "currency": currency,
            "status": McpCardStatus.PENDING,
        })
        logger.info(f"mcp.card correlation_id={correlation_id} state=CHECKOUT_CREATED "
                    f"order_id={order.id} payment_reference={checkout.id}")

        # ---- exactly one Stripe Checkout Session, keyed by the same stable identity
        try:
            session = self.__gateway.create_checkout_session(
                amount_minor=amount_minor,
                currency=currency,
                product_name=self.__product_name(bundle),
                metadata=self.__safe_metadata(checkout_id=checkout.id, order_id=order.id,
                                              user_id=user.id, bundle_code=request.bundle_code,
                                              quote_reference=request.quote_reference,
                                              device_id=device_id, amount_minor=amount_minor),
                idempotency_key=f"mcp-card-{key_hash}",
                client_reference_id=checkout.id,
                customer_email=getattr(user, "email", None))
        except StripeGatewayError as e:
            return self.__handle_gateway_error(error=e, record_id=record_id, user=user,
                                               checkout_id=checkout.id, order_id=order.id,
                                               currency=currency, amount_minor=amount_minor,
                                               correlation_id=correlation_id)

        self.__checkout_repo.attach_session(
            checkout_id=checkout.id, session_id=session.session_id,
            checkout_url=session.checkout_url, expires_at=session.expires_at,
            payment_intent_id=session.payment_intent_id)
        self.__user_order_repo.update_by({"id": order.id},
                                         data={"payment_intent_code": session.payment_intent_id
                                                                      or session.session_id})

        response = McpCardCheckoutResponse(
            payment_reference=checkout.id, order_id=order.id, checkout_url=session.checkout_url,
            status=McpCardStatus.PENDING, amount=self.__major_units(amount_minor),
            currency=currency, expires_at=session.expires_at, idempotent_replay=False,
            correlation_id=correlation_id)

        self.__persist_terminal(record_id=record_id, user=user, status=McpIdempotencyStatus.SUCCEEDED,
                                http_status=200, response=response, error_code=None,
                                order_id=order.id, correlation_id=correlation_id)
        logger.info(f"mcp.card correlation_id={correlation_id} state=SESSION_CREATED "
                    f"payment_reference={checkout.id} expires_in_min={session_expiry_minutes()}")
        return McpCardResult(envelope=ResponseHelper.success_data_response(response, 0),
                             http_status=200, idempotent_replay=False)

    async def __preflight(self, bundle_code: str):
        """Re-fetch the bundle and validate availability. Mirrors the legacy checks."""
        bundle = await self.__esim_hub_service.get_bundle_by_id(bundle_id=bundle_code)
        if not bundle or not bundle.is_active:
            raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                  details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        if not bundle.is_stockable:
            applicable = await self.__esim_hub_service.check_bundle_applicable(bundle.bundle_info_code)
            if not applicable:
                raise CustomException(code=400, name=ErrorMessages.BUNDLE_NOT_AVAILABLE,
                                      details=ErrorMessages.BUNDLE_NOT_AVAILABLE)
        return bundle

    @staticmethod
    def __authoritative_amount_minor(price: object) -> int:
        """Server-side price in the smallest currency unit, computed with Decimal only."""
        amount = Decimal(str(price if price is not None else "0"))
        return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def __major_units(amount_minor: Optional[int]) -> str:
        value = Decimal(str(amount_minor or 0)) / Decimal("100")
        return str(value.quantize(Decimal("0.01")))

    @staticmethod
    def __product_name(bundle) -> str:
        name = getattr(bundle, "display_title", None) or getattr(bundle, "bundle_name", None)
        return str(name or "eSIM bundle")[:250]

    @staticmethod
    def __safe_metadata(checkout_id: str, order_id: str, user_id: str, bundle_code: str,
                        quote_reference: str, device_id: Optional[str],
                        amount_minor: int) -> dict:
        """The same metadata payload the legacy Card flow puts on its PaymentIntent.

        Keys ``order_id``, ``user_id``, ``device_id``, ``bundle_code``, ``order_type``,
        ``env``, ``rule_id`` and ``amount`` mirror ``UserBundleService.__handle_card_payment``
        exactly, so support and reconciliation see one consistent shape across both
        flows and the legacy handler's field expectations are all satisfied.

        Two MCP-only additions: ``mcp_source`` (the routing marker - without it the
        legacy handler would fulfil these payments with no dedupe) and ``checkout_id`` /
        ``quote_reference`` for correlation.

        ``promo_code`` is omitted rather than sent as null: MCP card checkout never
        applies a promotion, and Stripe rejects null metadata values.

        Deliberately absent: access tokens, email/OTP, the raw or hashed
        Idempotency-Key, any Stripe secret, msisdn and any other personal data.
        """
        return {
            METADATA_SOURCE_KEY: METADATA_SOURCE_VALUE,
            "order_id": str(order_id),
            "user_id": str(user_id),
            "device_id": str(device_id or ""),
            "bundle_code": str(bundle_code)[:200],
            "order_type": str(UserOrderType.ASSIGN),
            "env": os.environ.get("ENVIRONMENT", "DEV"),
            "rule_id": "0",
            "amount": str(int(amount_minor)),
            "checkout_id": str(checkout_id),
            "quote_reference": str(quote_reference)[:128],
        }

    # ------------------------------------------------------------ failure paths

    def __handle_gateway_error(self, error: StripeGatewayError, record_id: str, user: UserModel,
                               checkout_id: str, order_id: str, currency: str, amount_minor: int,
                               correlation_id: str) -> McpCardResult:
        """Translate a typed gateway failure into a safe caller response.

        An *ambiguous* failure (timeout / connection loss) is the important case: Stripe
        may or may not hold a Session for our idempotency key. We never create another
        one with a new key. The record is left AMBIGUOUS and the caller is told to retry
        the very same Idempotency-Key, which replays the same Stripe idempotency key and
        therefore returns the original Session if it exists.
        """
        logger.error(f"mcp.card correlation_id={correlation_id} state=SESSION_FAILED "
                     f"category={error.category} ambiguous={error.ambiguous}")

        if error.ambiguous:
            self.__checkout_repo.transition(
                checkout_id=checkout_id,
                from_statuses=[McpCardStatus.PENDING],
                to_status=McpCardStatus.AMBIGUOUS,
                extra={"failure_code": error.category})
            response = McpCardCheckoutResponse(
                payment_reference=checkout_id, order_id=order_id, checkout_url=None,
                status=McpCardStatus.AMBIGUOUS, amount=self.__major_units(amount_minor),
                currency=currency, idempotent_replay=False, correlation_id=correlation_id,
                message=("The checkout could not be confirmed. Retry with the very same "
                         "Idempotency-Key; do not start a new checkout."))
            self.__persist_terminal(record_id=record_id, user=user,
                                    status=McpIdempotencyStatus.FAILED_RETRYABLE, http_status=503,
                                    response=response,
                                    error_code=str(McpCardErrorMessages.MCP_CARD_CHECKOUT_UNAVAILABLE),
                                    order_id=order_id, correlation_id=correlation_id)
            return McpCardResult(
                envelope=self.__failure_envelope(response, 503,
                                                 str(McpCardErrorMessages.MCP_CARD_CHECKOUT_UNAVAILABLE)),
                http_status=503, idempotent_replay=False)

        self.__checkout_repo.transition(checkout_id=checkout_id,
                                        from_statuses=[McpCardStatus.PENDING],
                                        to_status=McpCardStatus.FAILED,
                                        extra={"failure_code": error.category})
        response = McpCardCheckoutResponse(
            payment_reference=checkout_id, order_id=order_id, checkout_url=None,
            status=McpCardStatus.FAILED, amount=self.__major_units(amount_minor), currency=currency,
            idempotent_replay=False, correlation_id=correlation_id,
            message="The checkout could not be created. Start a new checkout.")
        self.__persist_terminal(record_id=record_id, user=user,
                                status=McpIdempotencyStatus.FAILED_FINAL, http_status=502,
                                response=response,
                                error_code=str(McpCardErrorMessages.MCP_CARD_CHECKOUT_UNAVAILABLE),
                                order_id=order_id, correlation_id=correlation_id)
        return McpCardResult(
            envelope=self.__failure_envelope(response, 502,
                                             str(McpCardErrorMessages.MCP_CARD_CHECKOUT_UNAVAILABLE)),
            http_status=502, idempotent_replay=False)

    def __fail(self, record_id: str, user: UserModel, http_status: int, error_code: str,
               correlation_id: str, status: str, message: str, currency: str) -> McpCardResult:
        response = McpCardCheckoutResponse(
            payment_reference=record_id or "", order_id=None, checkout_url=None,
            status=McpCardStatus.FAILED, amount="0.00", currency=currency,
            idempotent_replay=False, correlation_id=correlation_id, message=message)
        self.__persist_terminal(record_id=record_id, user=user, status=status,
                                http_status=http_status, response=response, error_code=error_code,
                                order_id=None, correlation_id=correlation_id)
        return McpCardResult(envelope=self.__failure_envelope(response, http_status, error_code),
                             http_status=http_status, idempotent_replay=False)

    # ------------------------------------------------------------------ replay

    def __replay(self, claim: McpIdempotencyClaim, correlation_id: str) -> McpCardResult:
        body = claim.response_body or {}
        http_status = claim.response_code or 200
        if not body:
            return self.__unresolved(correlation_id)
        try:
            response = McpCardCheckoutResponse(**body)
        except Exception as e:
            logger.error(f"mcp.card correlation_id={correlation_id} stored response unreadable: {redact(e)}")
            return self.__unresolved(correlation_id)

        response.idempotent_replay = True
        response.correlation_id = correlation_id
        logger.info(f"mcp.card correlation_id={correlation_id} state=REPLAY "
                    f"stored_status={claim.status} http={http_status}")
        if http_status == 200:
            envelope = ResponseHelper.success_data_response(response, 0)
        else:
            envelope = self.__failure_envelope(response, http_status, claim.error_code)
        return McpCardResult(envelope=envelope, http_status=http_status, idempotent_replay=True)

    def __reconcile_if_ambiguous(self, user: UserModel, claim: McpIdempotencyClaim, currency: str,
                                 key_hash: str, request: McpCardCheckoutRequest,
                                 correlation_id: str,
                                 device_id: Optional[str] = None) -> Optional[McpCardResult]:
        """Resolve a checkout left AMBIGUOUS by a Stripe timeout.

        Returns ``None`` when there is nothing to reconcile, so the caller falls through
        to the ordinary replay path.

        Safety: the retry reuses the identical Stripe idempotency key, so Stripe either
        hands back the Session it already created or creates the one it never did. There
        is no branch here that can produce a second Session or a second charge.
        """
        if not claim.record_id:
            return None
        try:
            record = self.__checkout_repo.get_by_record_id(idempotency_record_id=claim.record_id,
                                                           user_id=user.id)
        except Exception as e:
            logger.error(f"mcp.card correlation_id={correlation_id} reconcile lookup failed: {redact(e)}")
            return None

        if record is None or record.status != McpCardStatus.AMBIGUOUS:
            return None

        # The earlier attempt did get a Session; just hand it back.
        if record.stripe_session_id and record.checkout_url:
            self.__checkout_repo.transition(checkout_id=record.id,
                                            from_statuses=[McpCardStatus.AMBIGUOUS],
                                            to_status=McpCardStatus.PENDING)
            return self.__reconciled_result(record=record, checkout_url=record.checkout_url,
                                            expires_at=record.expires_at,
                                            correlation_id=correlation_id, user=user,
                                            claim=claim)

        try:
            bundle_name = request.bundle_code
            session = self.__gateway.create_checkout_session(
                amount_minor=record.amount_minor,
                currency=record.currency,
                product_name=str(bundle_name)[:250],
                metadata=self.__safe_metadata(checkout_id=record.id, order_id=record.order_id,
                                              user_id=user.id, bundle_code=record.bundle_code,
                                              quote_reference=record.quote_reference or "",
                                              device_id=device_id,
                                              amount_minor=record.amount_minor),
                idempotency_key=f"mcp-card-{key_hash}",
                client_reference_id=record.id,
                customer_email=getattr(user, "email", None))
        except StripeGatewayError as e:
            logger.error(f"mcp.card correlation_id={correlation_id} reconcile failed "
                         f"category={e.category}")
            return None  # stay AMBIGUOUS; replay the stored response

        self.__checkout_repo.attach_session(
            checkout_id=record.id, session_id=session.session_id,
            checkout_url=session.checkout_url, expires_at=session.expires_at,
            payment_intent_id=session.payment_intent_id)
        self.__checkout_repo.transition(checkout_id=record.id,
                                        from_statuses=[McpCardStatus.AMBIGUOUS],
                                        to_status=McpCardStatus.PENDING)
        logger.info(f"mcp.card correlation_id={correlation_id} state=RECONCILED "
                    f"payment_reference={record.id}")
        return self.__reconciled_result(record=record, checkout_url=session.checkout_url,
                                        expires_at=session.expires_at,
                                        correlation_id=correlation_id, user=user, claim=claim)

    def __reconciled_result(self, record, checkout_url: Optional[str], expires_at: Optional[str],
                            correlation_id: str, user: UserModel,
                            claim: McpIdempotencyClaim) -> McpCardResult:
        response = McpCardCheckoutResponse(
            payment_reference=record.id, order_id=record.order_id, checkout_url=checkout_url,
            status=McpCardStatus.PENDING, amount=self.__major_units(record.amount_minor),
            currency=record.currency, expires_at=expires_at, idempotent_replay=False,
            correlation_id=correlation_id,
            message="Recovered the checkout from an unconfirmed earlier attempt")
        self.__persist_terminal(record_id=claim.record_id, user=user,
                                status=McpIdempotencyStatus.SUCCEEDED, http_status=200,
                                response=response, error_code=None, order_id=record.order_id,
                                correlation_id=correlation_id)
        return McpCardResult(envelope=ResponseHelper.success_data_response(response, 0),
                             http_status=200, idempotent_replay=False)

    def __handle_in_progress(self, user: UserModel, claim: McpIdempotencyClaim,
                             correlation_id: str) -> McpCardResult:
        """Another execution owns this key. Never start a second checkout.

        If that execution already produced a Session we can safely hand back the same
        one; otherwise the caller is told to retry the identical key.
        """
        record = None
        if claim.record_id:
            try:
                record = self.__checkout_repo.get_by_record_id(
                    idempotency_record_id=claim.record_id, user_id=user.id)
            except Exception as e:
                logger.error(f"mcp.card correlation_id={correlation_id} could not read checkout: {redact(e)}")

        if record is not None and record.stripe_session_id and record.checkout_url:
            response = McpCardCheckoutResponse(
                payment_reference=record.id, order_id=record.order_id,
                checkout_url=record.checkout_url, status=McpCardStatus(record.status),
                amount=self.__major_units(record.amount_minor), currency=record.currency,
                expires_at=record.expires_at, idempotent_replay=True,
                correlation_id=correlation_id,
                message="Recovered the checkout created by an earlier attempt")
            return McpCardResult(envelope=ResponseHelper.success_data_response(response, 0),
                                 http_status=200, idempotent_replay=True)

        raise CustomException(
            code=409, name=McpErrorMessages.IDEMPOTENT_REQUEST_IN_PROGRESS,
            details=("A checkout for this Idempotency-Key is still being created. Retry the very "
                     "same Idempotency-Key. Do not create a new key and do not start a second "
                     "checkout."))

    def __unresolved(self, correlation_id: str) -> McpCardResult:
        response = McpCardCheckoutResponse(
            payment_reference="", status=McpCardStatus.AMBIGUOUS, amount="0.00",
            currency=system_currency(), idempotent_replay=True, correlation_id=correlation_id,
            message="Previous checkout state could not be resolved. Do not retry this purchase.")
        return McpCardResult(
            envelope=self.__failure_envelope(response, 424,
                                             str(McpCardErrorMessages.MCP_CARD_CHECKOUT_UNAVAILABLE)),
            http_status=424, idempotent_replay=True)

    # ------------------------------------------------------------- persistence

    def __mark_side_effect(self, record_id: str, user_id: str, order_id: str, correlation_id: str):
        """An order now exists for this key, so the key may never be re-executed."""
        try:
            self.__idempotency_repo.attach_order(record_id=record_id, user_id=user_id,
                                                 order_id=order_id)
        except Exception as e:
            logger.error(f"mcp.card correlation_id={correlation_id} "
                         f"failed to persist order correlation: {redact(e)}")

    def __persist_terminal(self, record_id: str, user: UserModel, status: str, http_status: int,
                           response: McpCardCheckoutResponse, error_code: Optional[str],
                           order_id: Optional[str], correlation_id: str):
        body = response.model_dump(mode="json")
        body["idempotent_replay"] = False
        body["correlation_id"] = None
        try:
            self.__idempotency_repo.mark_terminal(record_id=record_id, user_id=user.id, status=status,
                                                  response_code=http_status, response_body=body,
                                                  error_code=error_code, order_id=order_id)
        except Exception as e:
            logger.error(f"mcp.card correlation_id={correlation_id} "
                         f"failed to persist terminal state {status}: {redact(e)}")

    # --------------------------------------------------------------- envelopes

    @staticmethod
    def __failure_envelope(response: McpCardCheckoutResponse, http_status: int,
                           error_code: Optional[str]) -> Response:
        title = translate(error_code) if error_code else None
        return Response(status="failed", totalCount=0, data=response, title=title,
                        message=response.message or title, developerMessage=error_code,
                        responseCode=http_status)

    @staticmethod
    def __status_message(status: McpCardStatus) -> Optional[str]:
        if status == McpCardStatus.AMBIGUOUS:
            return "Payment may have been taken without a completed eSIM. Contact support."
        if status == McpCardStatus.EXPIRED:
            return "The checkout session expired. Start a new checkout with a new Idempotency-Key."
        if status == McpCardStatus.CANCELLED:
            return "The checkout was cancelled."
        if status == McpCardStatus.FAILED:
            return "The payment did not succeed. Start a new checkout with a new Idempotency-Key."
        return None
