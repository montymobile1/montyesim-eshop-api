"""Webhook processing for MCP card checkouts.

This is the ONLY thing that may conclude a payment succeeded. A browser landing on
``success_url`` proves nothing and is never consulted here.

Isolation from the legacy webhook
---------------------------------
The legacy handler processes ``payment_intent.*`` and early-returns on everything else,
so ``checkout.session.*`` has always been a no-op for it. This service claims exactly
those four event types, and only when the flag is on and the Session carries our own
``mcp_source`` marker. A Session created by anything else - or by a future product -
cannot drive MCP provisioning.

Ordering and duplication
------------------------
Stripe guarantees neither once-only delivery nor ordering. Both are handled here:
  * every event id is claimed in a dedupe ledger before anything happens;
  * every state change is a compare-and-set against the states it is legal to leave,
    so a late ``expired`` can never demote a ``COMPLETED`` record.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from app.config.db import OrderStatusEnum, PaymentTypeEnum
from app.config.feature_flags import is_mcp_card_purchase_enabled
from app.config.mcp_card_constants import (
    MCP_CARD_WEBHOOK_EVENTS,
    METADATA_SOURCE_KEY,
    METADATA_SOURCE_VALUE,
    PAYMENT_INTENT_CANCELED,
    PAYMENT_INTENT_FAILED,
    PAYMENT_INTENT_PAYMENT_FAILED,
    PAYMENT_INTENT_SUCCEEDED,
    McpCardStatus,
)
from app.models.mcp_card import McpCardCheckoutModel
from app.repo import UserOrderRepo, UserProfileRepo
from app.repo.mcp_card_checkout_repo import McpCardCheckoutRepo, McpStripeWebhookEventRepo
from app.schemas.home import BundleDTO
from app.services.bundle_service import BundleService
from app.services.mcp_stripe_gateway import redact


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class McpCardWebhookService:

    def __init__(self):
        self.__checkout_repo = McpCardCheckoutRepo()
        self.__event_repo = McpStripeWebhookEventRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_profile_repo = UserProfileRepo()
        self.__bundle_service = BundleService()

    # ------------------------------------------------------------------ routing

    def handles(self, event: Any) -> bool:
        """True only for ``payment_intent.*`` events that WE created.

        Both flows now ride the same event type, so the discriminator is the
        ``mcp_source`` marker inside the PaymentIntent metadata - never the type alone.
        A legacy PaymentIntent has no marker and is therefore never claimed here, which
        is what keeps the legacy path byte-for-byte unchanged.

        Deliberately NOT gated on the feature flag. The flag governs *creating* new
        checkouts; a payment already in flight must still be completed if the flag is
        turned off, otherwise a customer who has been charged would never be provisioned
        and the legacy handler - which has no dedupe - would pick it up instead.
        """
        event_type = self.__field(event, "type")
        if not event_type or event_type not in MCP_CARD_WEBHOOK_EVENTS:
            return False
        metadata = self.__metadata_of(self.__session_of(event))
        return metadata.get(METADATA_SOURCE_KEY) == METADATA_SOURCE_VALUE

    async def handle_event(self, event: Dict[str, Any]) -> Dict[str, str]:
        """Process one verified Stripe event. Always returns a small, safe dict.

        Async because the webhook route itself is async and provisioning is awaited
        inline: the alternative - handing the work to a background executor as the
        legacy handler does - would make "provisioned exactly once" unobservable to
        Stripe, which retries precisely when it does not get a 2xx.
        """
        event_id = self.__field(event, "id")
        event_type = self.__field(event, "type")
        session = self.__session_of(event)
        metadata = self.__metadata_of(session)

        if metadata.get(METADATA_SOURCE_KEY) != METADATA_SOURCE_VALUE:
            # Not ours. Never act on a Session this feature did not create.
            logger.info(f"mcp.card.webhook ignoring foreign session event type={event_type}")
            return {"result": "ignored_not_mcp"}

        if not event_id:
            logger.warning("mcp.card.webhook event without an id, refusing to process")
            return {"result": "ignored_no_event_id"}

        checkout_id = str(metadata.get("checkout_id") or "")

        # ---- duplicate suppression happens before any state is touched
        if not self.__event_repo.claim(event_id=event_id, event_type=str(event_type),
                                       checkout_id=checkout_id or None):
            logger.info(f"mcp.card.webhook duplicate event suppressed type={event_type}")
            return {"result": "duplicate"}

        record = self.__load_record(session=session, checkout_id=checkout_id)
        if record is None:
            logger.warning(f"mcp.card.webhook no checkout record for event type={event_type}")
            self.__event_repo.mark_result(event_id, "no_record")
            return {"result": "ignored_unknown_checkout"}

        try:
            if event_type == PAYMENT_INTENT_SUCCEEDED:
                result = await self.__handle_paid(record=record, session=session, metadata=metadata)
            elif event_type in (PAYMENT_INTENT_PAYMENT_FAILED, PAYMENT_INTENT_FAILED):
                result = self.__handle_failed(record=record)
            elif event_type == PAYMENT_INTENT_CANCELED:
                result = self.__handle_cancelled(record=record)
            else:  # unreachable: handles() gates the type
                result = {"result": "ignored_unsupported"}
        except Exception as e:
            logger.error(f"mcp.card.webhook processing error type={event_type}: {redact(e)}")
            self.__event_repo.mark_result(event_id, "error")
            # Returning rather than raising keeps a 2xx going back to Stripe only when
            # the caller decides so; the record is left in a reconcilable state.
            return {"result": "error"}

        self.__event_repo.mark_result(event_id, result.get("result", "ok"))
        return result

    # ------------------------------------------------------------- event paths

    async def __handle_paid(self, record: McpCardCheckoutModel, session: Dict[str, Any],
                            metadata: Dict[str, str]) -> Dict[str, str]:
        """``session`` here is the PaymentIntent object carried by the event."""
        intent_status = str(self.__field(session, "status") or "").lower()
        if intent_status and intent_status != "succeeded":
            # e.g. requires_action / processing: an async method has not settled yet.
            logger.info(f"mcp.card.webhook intent not settled status={intent_status}, waiting")
            return {"result": "awaiting_payment"}

        verified, reason = self.__verify(record=record, session=session, metadata=metadata)
        if not verified:
            logger.error(f"mcp.card.webhook verification failed reason={reason}")
            self.__checkout_repo.transition(
                checkout_id=record.id,
                from_statuses=[McpCardStatus.PENDING, McpCardStatus.AMBIGUOUS],
                to_status=McpCardStatus.AMBIGUOUS, extra={"failure_code": reason})
            return {"result": "verification_failed"}

        payment_intent_id = self.__field(session, "id")

        # PENDING -> PAID. A record already past PENDING (out-of-order or replayed
        # delivery that slipped the ledger) is not moved backwards.
        claimed_paid = self.__checkout_repo.transition(
            checkout_id=record.id, from_statuses=[McpCardStatus.PENDING],
            to_status=McpCardStatus.PAID,
            extra={"paid_at": _now(),
                   "stripe_payment_intent_id": payment_intent_id
                                               or record.stripe_payment_intent_id})
        if not claimed_paid and record.status not in (McpCardStatus.PAID,):
            logger.info(f"mcp.card.webhook not transitioning from status={record.status}")
            return {"result": "already_terminal"}

        self.__user_order_repo.update_by({"id": record.order_id},
                                         data={"payment_status": OrderStatusEnum.SUCCESS,
                                               "payment_time": _now()})
        return await self.__provision(record=record)

    def __handle_failed(self, record: McpCardCheckoutModel) -> Dict[str, str]:
        # Only a not-yet-successful checkout can fail. PAID is excluded: a late failure
        # event must never demote a payment we have already verified as succeeded.
        moved = self.__checkout_repo.transition(
            checkout_id=record.id, from_statuses=[McpCardStatus.PENDING],
            to_status=McpCardStatus.FAILED, extra={"failure_code": "PAYMENT_FAILED"})
        if moved and record.order_id:
            self.__user_order_repo.update_by({"id": record.order_id},
                                             data={"payment_status": OrderStatusEnum.FAILURE})
        # No refund is issued here: no existing verified business rule requires one, and
        # a failed intent means no money was captured.
        return {"result": "failed" if moved else "ignored_terminal"}

    def __handle_cancelled(self, record: McpCardCheckoutModel) -> Dict[str, str]:
        """``payment_intent.canceled`` - Stripe cancels the intent when the hosted
        Checkout Session expires unpaid, which is how expiry reaches us now that this
        feature no longer subscribes to ``checkout.session.expired``."""
        moved = self.__checkout_repo.transition(
            checkout_id=record.id, from_statuses=[McpCardStatus.PENDING],
            to_status=McpCardStatus.EXPIRED, extra={"failure_code": "SESSION_EXPIRED"})
        if moved and record.order_id:
            self.__user_order_repo.update_by({"id": record.order_id},
                                             data={"payment_status": OrderStatusEnum.FAILURE,
                                                   "order_status": OrderStatusEnum.CANCELED})
        return {"result": "expired" if moved else "ignored_terminal"}

    # ------------------------------------------------------------- provisioning

    async def __provision(self, record: McpCardCheckoutModel) -> Dict[str, str]:
        """Assign the eSIM at most once.

        The PAID -> PROVISIONING compare-and-set is the exactly-once gate: two concurrent
        deliveries both reach here, but only one observes a PAID row and proceeds.
        """
        claimed = self.__checkout_repo.transition(
            checkout_id=record.id, from_statuses=[McpCardStatus.PAID],
            to_status=McpCardStatus.PROVISIONING)
        if not claimed:
            logger.info("mcp.card.webhook provisioning already claimed elsewhere")
            return {"result": "already_provisioning"}

        order = self.__user_order_repo.get_by_id(record.order_id)
        if order is None:
            self.__checkout_repo.transition(checkout_id=record.id,
                                            from_statuses=[McpCardStatus.PROVISIONING],
                                            to_status=McpCardStatus.AMBIGUOUS,
                                            extra={"failure_code": "ORDER_MISSING"})
            return {"result": "ambiguous_order_missing"}

        try:
            bundle = BundleDTO.model_validate_json(order.bundle_data)
            result = await self.__bundle_service.buy_bundle(
                user_order=order, bundle=bundle, user_id=record.user_id,
                payment_status=OrderStatusEnum.SUCCESS, payment_type=PaymentTypeEnum.CARD,
                rule_id=None)
            # buy_bundle signals provisioning failure by *returning* an exception rather
            # than raising it, so both shapes have to be treated as failure.
            failed = result is None or isinstance(result, Exception)
        except Exception as e:
            logger.error(f"mcp.card.webhook provisioning raised: {redact(e)}")
            failed = True

        if failed:
            # Paid but not provisioned. Preserve a recoverable state; never charge again,
            # never auto-refund, never retry automatically.
            self.__checkout_repo.transition(checkout_id=record.id,
                                            from_statuses=[McpCardStatus.PROVISIONING],
                                            to_status=McpCardStatus.AMBIGUOUS,
                                            extra={"failure_code": "PROVISIONING_FAILED"})
            logger.error("mcp.card.webhook PAID BUT NOT PROVISIONED - manual reconciliation needed")
            return {"result": "ambiguous_provisioning_failed"}

        self.__checkout_repo.transition(checkout_id=record.id,
                                        from_statuses=[McpCardStatus.PROVISIONING],
                                        to_status=McpCardStatus.COMPLETED,
                                        extra={"provisioned_at": _now()})
        logger.info("mcp.card.webhook provisioned successfully")
        return {"result": "provisioned"}

    # -------------------------------------------------------------- verification

    def __verify(self, record: McpCardCheckoutModel, session: Dict[str, Any],
                 metadata: Dict[str, str]) -> Tuple[bool, str]:
        """Everything must agree before a single eSIM is handed out."""
        if str(metadata.get("user_id") or "") != str(record.user_id):
            return False, "USER_MISMATCH"
        if str(metadata.get("order_id") or "") != str(record.order_id or ""):
            return False, "ORDER_MISMATCH"
        if str(metadata.get("bundle_code") or "") != str(record.bundle_code):
            return False, "BUNDLE_MISMATCH"

        # A PaymentIntent reports `amount` (and `amount_received` once captured); a
        # Checkout Session reported `amount_total`. Accept whichever the payload has so
        # the check is exact for the event we now consume.
        amount = self.__field(session, "amount_received") or self.__field(session, "amount")
        if amount is None:
            amount = self.__field(session, "amount_total")
        if amount is None or int(amount) != int(record.amount_minor):
            return False, "AMOUNT_MISMATCH"

        currency = str(self.__field(session, "currency") or "").upper()
        if currency != str(record.currency or "").upper():
            return False, "CURRENCY_MISMATCH"

        intent_id = self.__field(session, "id")
        if record.stripe_payment_intent_id and intent_id \
                and str(intent_id) != str(record.stripe_payment_intent_id):
            return False, "PAYMENT_INTENT_MISMATCH"

        if not record.order_id:
            return False, "ORDER_MISSING"
        order = self.__user_order_repo.get_by_id(record.order_id)
        if order is None or str(order.user_id) != str(record.user_id):
            return False, "ORDER_OWNER_MISMATCH"
        if str(order.bundle_id) != str(record.bundle_code):
            return False, "ORDER_BUNDLE_MISMATCH"

        return True, "OK"

    # -------------------------------------------------------------------- utils

    def __load_record(self, session: Dict[str, Any], checkout_id: str) -> Optional[McpCardCheckoutModel]:
        """Find the checkout this PaymentIntent belongs to.

        By intent id first (recorded when the Session was created), falling back to the
        ``checkout_id`` we stamped into the metadata ourselves.
        """
        intent_id = self.__field(session, "id")
        record = None
        if intent_id:
            record = self.__checkout_repo.get_by_payment_intent_id(str(intent_id))
        if record is None and checkout_id:
            record = self.__checkout_repo.get_first_by(where={"id": checkout_id})
        return record

    @staticmethod
    def __field(obj: Any, name: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def __session_of(self, event: Any) -> Dict[str, Any]:
        data = self.__field(event, "data") or {}
        return self.__field(data, "object") or {}

    def __metadata_of(self, session: Any) -> Dict[str, str]:
        metadata = self.__field(session, "metadata") or {}
        if not isinstance(metadata, dict):
            try:
                metadata = dict(metadata)
            except Exception:
                return {}
        return {str(k): str(v) for k, v in metadata.items()}
