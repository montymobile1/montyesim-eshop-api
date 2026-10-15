"""Inbound LinkAja notify (Inform Payment, spec v1.1 §4.3) handling.

Deliberately a separate service and a separate route from the Stripe webhook
(`CallbackService.handle_payment_webhook` / `POST /callback/payment-webhook`).
Nothing here touches Stripe, and Stripe's handler is untouched by this file.

## Reserve-then-commit

The §3.1 flow diagram (p.7) shows LinkAja reserving the payment, informing us,
and committing it only after our acknowledgement. So the notify handler must
answer fast: it verifies the signature, parses and reconciles the body, claims
the order (pending -> success, atomically) and replies 2005600. Provisioning
the eSIM through `BundleService` is slow (eSIM Hub round-trips) and is run
AFTER the response, on the existing `TaskExecutor` queue - the same mechanism
the Stripe webhook uses.

## Fail-closed guarantee

`LinkAjaService.verify_callback` raises until LinkAja's notify public key is
configured, and raises on any signature that does not verify. Because
verification happens *before* anything else in `handle_callback`, an
unauthenticated request can never reach the order or `BundleService`. An
unauthenticated provisioning webhook would let anyone mint free eSIMs.

## Idempotency

The duplicate guard is the conditional UPDATE `payment_status = pending ->
success`: of N deliveries of the same notify exactly one claims the row and
schedules provisioning; the others are acknowledged with the same 2005600 so
LinkAja stops retrying.

## Reconciliation

LinkAja does not retry a notify. `reconcile_from_status` is the entry point
for the reconciliation job (`LinkAjaReconciliationService`): it applies a
status inquiry answer to a pending order through the same atomic claim, so a
notify and the job can never both provision the same order.

## Response contract

The notify route answers in LinkAja's format, not our API envelope: on success
exactly `{"responseCode":"2005600","responseMessage":"Successful",
"approvalCode":"<echoed>"}` (p.15-16); on failure the §5.2 code for service 56
(e.g. 4015600 invalid signature, 4045601 unknown order).
"""

import asyncio
import threading
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.config.constants import ErrorMessages
from app.config.db import OrderStatusEnum, PaymentTypeEnum, UserOrderType
from app.exceptions import CustomException
from app.models.user import UserOrderModel
from app.repo import UserOrderRepo
from app.schemas.home import BundleDTO
from app.services.bundle_service import BundleService
from app.services.integration.link_aja import (
    NON_FINAL_STATUSES,
    NOTIFY_SUCCESS_CODE,
    NOTIFY_SUCCESS_MESSAGE,
    LinkAjaCallbackInvalidException,
    LinkAjaCallbackResult,
    LinkAjaCallbackVerificationException,
    LinkAjaException,
    LinkAjaNotifyAck,
    LinkAjaOrderNotFoundException,
    LinkAjaPaymentStatus,
    LinkAjaService,
    LinkAjaStatusResult,
    is_provisioning_status,
    read_order_data,
)
from app.services.task_executor import TaskExecutor


def notify_success_body(approval_code: str) -> dict:
    """Exactly what LinkAja expects back (p.15-16), key order included."""
    return {"responseCode": NOTIFY_SUCCESS_CODE, "responseMessage": NOTIFY_SUCCESS_MESSAGE,
            "approvalCode": approval_code}


def notify_error_response(exc: Exception) -> JSONResponse:
    """Translate a failure into LinkAja's SNAP error shape (§5.2).

    Our API envelope is never used on this route: LinkAja parses responseCode.
    Details are deliberately not echoed to the caller.
    """
    if isinstance(exc, LinkAjaException):
        status, code, message = exc.code, exc.snap_response_code, exc.snap_response_message
    else:
        status, code, message = 500, "5005600", "General Error"
    return JSONResponse(status_code=status, content={"responseCode": code, "responseMessage": message})


class LinkAjaCallbackService:
    """Turns an authenticated LinkAja notify into (at most one) provisioning."""

    def __init__(self, link_aja_service: LinkAjaService = None,
                 user_order_repo: UserOrderRepo = None,
                 bundle_service: BundleService = None,
                 task_executor: Optional[TaskExecutor] = None):
        # Lazily defaulted so constructing this never requires LinkAja config.
        self.__link_aja_service = link_aja_service or LinkAjaService()
        self.__user_order_repo = user_order_repo or UserOrderRepo()
        self.__bundle_service = bundle_service or BundleService()
        self.__task_executor = task_executor or TaskExecutor()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle_callback(self, request: Request) -> JSONResponse:
        """Authenticate, reconcile and acknowledge an inbound LinkAja notify.

        Verification comes first and is not optional - see the module docstring.
        Every outcome is rendered in LinkAja's response format.
        """
        raw_body = await request.body()
        headers = dict(request.headers)
        path = request.url.path
        # The query string (?order_id=..., see return_url.py) only matters for
        # the signature's EndpointUrl. The order is always taken from the
        # signed body (originalPartnerReferenceNo), never from the query.
        query = request.url.query or ""

        logger.info(f"[LinkAja] notify received on {path} ({len(raw_body)} bytes)")

        try:
            # Fails closed: nothing below this line runs for an unauthenticated request.
            result = self.__link_aja_service.verify_callback(raw_body=raw_body, headers=headers,
                                                             request_path=path, query_string=query,
                                                             request_url=str(request.url))
            ack = await self.process_verified_callback(result)
        except (LinkAjaException, CustomException) as exc:
            logger.error(f"[LinkAja] notify rejected: {exc.name}: {exc.details}")
            return notify_error_response(exc)

        return JSONResponse(status_code=200, content=notify_success_body(ack.approval_code))

    # ------------------------------------------------------------------
    # Post-verification pipeline
    # ------------------------------------------------------------------

    async def process_verified_callback(self, result: LinkAjaCallbackResult) -> LinkAjaNotifyAck:
        """Resolve the order, enforce idempotency, reconcile the amount, claim
        the order and schedule provisioning if - and only if - the payment is
        verified successful. Returns what to acknowledge with."""

        # 1. Never trust an unverified result, even internally.
        if not result.verified:
            logger.error(f"[LinkAja] refusing unverified callback for order {result.merchant_order_id}")
            raise LinkAjaCallbackVerificationException(
                details="LinkAja callback was not verified; refusing to process")

        order = self.__resolve_order(result)
        approval_code = result.approval_code or ""

        # 2. Idempotency fast path: already-successful payment, nothing to do.
        #    Acknowledged identically so LinkAja stops retrying.
        if order.payment_status == OrderStatusEnum.SUCCESS:
            logger.info(f"[LinkAja] duplicate notify for order {order.id}: payment already "
                        f"successful, not provisioning again")
            return LinkAjaNotifyAck(approval_code=approval_code)

        # 3. Only a verified SUCCESS may provision. Everything else - PENDING,
        #    NOT_FOUND, UNKNOWN, FAILED, REFUNDED, ... - records state and stops.
        if not is_provisioning_status(result.status):
            self.__record_non_success(order=order, status=result.status)
            return LinkAjaNotifyAck(approval_code=approval_code)

        # 4. The amount LinkAja collected must be the amount we asked for.
        self.__reconcile_amount(order=order, result=result)

        # 5. Atomically claim the order. This is the real duplicate guard: the
        #    UPDATE ... WHERE payment_status = 'pending' is atomic in Postgres,
        #    so of two concurrent success notifies exactly one matches a row.
        if not self.__claim_for_provisioning(order=order, provider_reference=result.provider_reference):
            logger.info(f"[LinkAja] concurrent duplicate notify for order {order.id}: "
                        f"another notify already claimed it")
            return LinkAjaNotifyAck(approval_code=approval_code)

        # 6. Provision AFTER the response: LinkAja commits the payment on our
        #    acknowledgement (p.7), so the slow eSIM Hub work must not delay it.
        self.__schedule_provisioning(order=order)
        return LinkAjaNotifyAck(approval_code=approval_code, provisioning_scheduled=True)

    async def reconcile_from_status(self, order: UserOrderModel, status: LinkAjaStatusResult) -> bool:
        """Apply a status inquiry answer (spec §4.4) to a pending LinkAja order.

        Used by the reconciliation job, because LinkAja does not retry a
        notify. The inquiry is our own authenticated call to LinkAja, keyed by
        our order id. It carries no amount, so instead of comparing amounts the
        order must hold the IDR quote stored when the payment was created: that
        quote is what LinkAja was asked to collect. Provisioning goes through
        the same atomic pending -> success claim as the notify, so whichever of
        the two runs second does nothing.

        Returns True when this call claimed the order and scheduled provisioning.
        """
        if order.payment_type != PaymentTypeEnum.LINK_AJA or order.payment_status != OrderStatusEnum.PENDING:
            return False

        if (status.provider_reference and order.payment_intent_code
                and status.provider_reference != order.payment_intent_code):
            logger.error(f"[LinkAja] reconciliation: provider reference mismatch for order {order.id}; "
                         f"leaving it pending")
            return False

        if not is_provisioning_status(status.status):
            if status.status in NON_FINAL_STATUSES or status.status == LinkAjaPaymentStatus.REFUNDED:
                return False
            logger.info(f"[LinkAja] reconciliation: order {order.id} is {status.status}; recording failure")
            # Guarded on pending so a success claimed in the meantime is never overwritten.
            self.__user_order_repo.update_by(
                where={"id": order.id, "payment_status": OrderStatusEnum.PENDING},
                data={"payment_status": OrderStatusEnum.FAILURE,
                      "order_status": OrderStatusEnum.FAILURE,
                      "callback_time": self.__now()})
            return False

        if read_order_data(order.bundle_data) is None:
            logger.error(f"[LinkAja] reconciliation: order {order.id} is paid per LinkAja but has no stored "
                         f"LinkAja amount; not provisioning, needs manual review")
            return False

        if not self.__claim_for_provisioning(order=order, provider_reference=status.provider_reference):
            logger.info(f"[LinkAja] reconciliation: order {order.id} was already claimed")
            return False

        logger.info(f"[LinkAja] reconciliation: order {order.id} paid per status inquiry; provisioning")
        self.__schedule_provisioning(order=order)
        return True

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def __resolve_order(self, result: LinkAjaCallbackResult) -> UserOrderModel:
        """Look the order up by our own reference and sanity-check it."""
        order = self.__user_order_repo.get_by_id(result.merchant_order_id)
        if not order:
            logger.error(f"[LinkAja] callback referenced unknown order {result.merchant_order_id}")
            raise LinkAjaOrderNotFoundException(
                details=f"No order found for LinkAja callback reference {result.merchant_order_id}")

        # Guard against a callback being applied to an order paid for by another
        # provider - that would let a LinkAja callback settle a Stripe order.
        if order.payment_type != PaymentTypeEnum.LINK_AJA:
            logger.error(f"[LinkAja] callback for order {order.id} whose payment_type is "
                         f"{order.payment_type}, not LinkAja")
            raise LinkAjaOrderNotFoundException(
                details=f"Order {order.id} was not paid with LinkAja")

        # If we stored a provider reference at creation time, it must match.
        if (result.provider_reference and order.payment_intent_code
                and result.provider_reference != order.payment_intent_code):
            logger.error(f"[LinkAja] provider reference mismatch for order {order.id}")
            raise LinkAjaOrderNotFoundException(
                details=f"LinkAja transaction reference does not match order {order.id}")

        return order

    @staticmethod
    def __reconcile_amount(order: UserOrderModel, result: LinkAjaCallbackResult) -> None:
        """The notified amount must equal the IDR amount stored on the order
        when the payment was created ("6000" == "6000.00").

        The stored quote is the only reference: the current
        LINKAJA_USD_TO_IDR_RATE is deliberately not consulted, so a rate change
        between creation and notify changes nothing. An order without a stored
        quote (or a notify without an amount) cannot be reconciled and is
        rejected - fail closed.
        """
        if result.amount is None:
            raise LinkAjaCallbackInvalidException(
                details=f"LinkAja notify for order {order.id} carries no amount to reconcile",
                snap_response_code="4005602", snap_response_message="Invalid Mandatory Field amount.value")
        quote = read_order_data(order.bundle_data)
        if quote is None:
            logger.error(f"[LinkAja] order {order.id} has no stored LinkAja amount; cannot reconcile the notify")
            raise LinkAjaCallbackInvalidException(
                details=f"Order {order.id} has no stored LinkAja amount to reconcile against",
                snap_response_code="4045613", snap_response_message="Invalid Amount")
        expected = Decimal(quote.amount)
        if result.amount != expected or (result.currency or "") != quote.currency:
            logger.error(f"[LinkAja] amount mismatch for order {order.id}: notified {result.amount} "
                         f"{result.currency}, stored {quote.amount} {quote.currency} "
                         f"(rate at creation {quote.usd_to_idr_rate})")
            raise LinkAjaCallbackInvalidException(
                details=f"LinkAja notify amount does not match order {order.id}",
                snap_response_code="4045613", snap_response_message="Invalid Amount")

    def __record_non_success(self, order: UserOrderModel, status: LinkAjaPaymentStatus) -> None:
        """Persist a non-provisioning outcome.

        Non-final statuses (pending / not found / unknown) leave the order
        pending: "not found" is simply "not paid yet" (spec §5.4), and an
        unmapped status means our map is incomplete, not that the payment
        failed. Failed / canceled / expired become failures. Refunded is logged
        and left alone: a refund of a pending order changes nothing here.
        """
        logger.info(f"[LinkAja] order {order.id} callback status {status}: not provisioning")

        if status in NON_FINAL_STATUSES:
            if status == LinkAjaPaymentStatus.UNKNOWN:
                logger.error(f"[LinkAja] unmapped provider status for order {order.id}; the "
                             f"provider->internal status map is incomplete. Leaving order pending")
            return

        if status == LinkAjaPaymentStatus.REFUNDED:
            logger.warning(f"[LinkAja] order {order.id} reported refunded while not successful; "
                           f"leaving it unchanged")
            return

        self.__user_order_repo.update_by(
            where={"id": order.id},
            data={"payment_status": OrderStatusEnum.FAILURE,
                  "order_status": OrderStatusEnum.FAILURE,
                  "callback_time": self.__now()})

    def __claim_for_provisioning(self, order: UserOrderModel, provider_reference: Optional[str]) -> bool:
        """Atomically transition payment_status pending -> success.

        Returns True for the caller that won the claim, False for duplicates.
        `order_status` is deliberately left alone: `BundleService.buy_bundle`
        owns it, and setting it here would mask a provisioning failure.
        """
        data = {
            "payment_status": OrderStatusEnum.SUCCESS,
            "payment_time": self.__now(),
            "callback_time": self.__now(),
        }
        if provider_reference and not order.payment_intent_code:
            data["payment_intent_code"] = provider_reference

        claimed = self.__user_order_repo.update_by(
            where={"id": order.id, "payment_status": OrderStatusEnum.PENDING},
            data=data)

        if claimed:
            # Keep the in-memory model consistent with what we just persisted,
            # since BundleService re-persists it from this object.
            order.payment_status = OrderStatusEnum.SUCCESS
        return bool(claimed)

    def __schedule_provisioning(self, order: UserOrderModel) -> None:
        """Queue provisioning on the shared task executor, exactly like the
        Stripe webhook does, so the 2005600 goes out first.

        The executor runs tasks on worker threads without an event loop, hence
        `asyncio.run`. If the queue is full the task is run on a dedicated
        thread instead: the order is already claimed as paid, and a paid order
        must never be left unprovisioned because of back-pressure.
        """
        order_id = order.id

        def task():
            logger.info(f"[LinkAja] provisioning order {order_id} after acknowledgement")
            return asyncio.run(self.provision(order))

        if not self.__task_executor.add_task(task):
            logger.error(f"[LinkAja] task executor queue is full; provisioning order {order_id} "
                         f"on a dedicated thread")
            self.__run_detached(task)

    @staticmethod
    def __run_detached(task: Callable[[], object]) -> None:
        threading.Thread(target=task, name="linkaja-provision", daemon=True).start()

    async def provision(self, order: UserOrderModel):
        """Route to the existing provisioning layer. No eSIM logic lives here.

        Public so the background task (and tests) can run exactly what the
        notify scheduled; it is never reachable from the network directly.
        """
        bundle = self.__load_bundle(order)

        if order.order_type == UserOrderType.ASSIGN:
            logger.info(f"[LinkAja] provisioning assign order {order.id}")
            return await self.__bundle_service.buy_bundle(
                user_order=order, bundle=bundle, user_id=order.user_id,
                payment_status=OrderStatusEnum.SUCCESS,
                payment_type=PaymentTypeEnum.LINK_AJA)

        if order.order_type == UserOrderType.BUNDLE_TOP_UP:
            # Top-ups are not enabled for LinkAja (see the report). Beyond the
            # business decision there is a concrete blocker: `top_up_bundle`
            # needs an `iccid`, which the Stripe flow carries in payment-intent
            # metadata and which `user_order` has no column for. Enabling LinkAja
            # top-ups requires deciding how the iccid is carried through the
            # provider round-trip. Failing loudly rather than provisioning wrongly.
            logger.error(f"[LinkAja] top-up order {order.id} reached provisioning but LinkAja "
                         f"top-ups are not supported")
            raise CustomException(
                code=503, name=ErrorMessages.LINKAJA_NOT_CONFIGURED,
                details="LinkAja top-up provisioning is not supported: the iccid carrier for a "
                        "LinkAja top-up round-trip has not been specified")

        logger.error(f"[LinkAja] order {order.id} has unsupported order_type {order.order_type}")
        raise CustomException(
            code=400, name=ErrorMessages.REQUEST_FAILED,
            details=f"Unsupported order type {order.order_type} for a LinkAja callback")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def __load_bundle(order: UserOrderModel) -> BundleDTO:
        """Rehydrate the bundle snapshot stored on the order at creation time."""
        if not order.bundle_data:
            raise CustomException(
                code=400, name=ErrorMessages.REQUEST_FAILED,
                details=f"Order {order.id} has no stored bundle data to provision")
        return BundleDTO.model_validate_json(order.bundle_data)

    @staticmethod
    def __now() -> str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
