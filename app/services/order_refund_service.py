"""Refund of a paid order whose eSIM Hub order / top-up failed.

Called only from failure paths: after `BundleService.buy_bundle` / `top_up_bundle` returned a
`HubOrderFailedException` (or raised) for an order the customer already paid. A successful purchase
never reaches this module.

Rules
-----
* Automatic refund only when the Hub *definitely* created nothing (HTTP 200 with "success": false,
  see app/services/integration/esim_hub_outcome.py) and nothing was provisioned for the order.
  Card: full Stripe refund of the PaymentIntent (exactly what was captured: after promo, incl. tax)
  plus a full reversal of the Stripe Tax transaction. Wallet: the exact debited amount credited back.
* Everything else is flagged for manual review and alerted: ambiguous Hub failures, DCB and LinkAja
  (no automatic refund for them yet), and a wallet credit that errored (the balance may have moved).
* Never twice: one `user_order_refund` row per order (primary key). The claim is
  INSERT ... ON CONFLICT DO NOTHING and every later change is conditional on the current status.
  The Stripe call also carries the idempotency key "refund-<order_id>".
* A failed Stripe refund is left in status `failed`; support retries it with `retry_failed_refund`.
  The retry uses the same idempotency key, so a refund that did go through is not repeated.

Nothing in here raises to the caller: a refund problem is logged and alerted, and the caller's own
response is unchanged.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Iterable, Optional, Tuple

import stripe
from loguru import logger
from soupsieve.util import lower

from app.config.config import get_email_template, send_email
from app.config.constants import UserWalletTransactionSource
from app.config.db import OrderRefundStatusEnum, PaymentTypeEnum
from app.config.helper import get_config
from app.config.notification_types import send_order_refunded_notification
from app.config.push_notification_manager import fcm_service
from app.exceptions import HubOrderFailedException
from app.models.order_refund import UserOrderRefundModel
from app.models.user import UserOrderModel
from app.repo import UserOrderRepo, UserProfileBundleRepo, UserRepo
from app.repo.order_refund_repo import OrderRefundRepo
from app.services.task_executor import TaskExecutor
from app.services.user_wallet_service import UserWalletService

STRIPE_REFUND_IDEMPOTENCY_PREFIX = "refund-"


class RefundResult(StrEnum):
    REFUNDED = "refunded"
    FAILED = "failed"                  # Stripe refund failed; retryable
    MANUAL_REVIEW = "manual_review"
    ALREADY_HANDLED = "already_handled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrderRefundService:

    def __init__(self, refund_repo: OrderRefundRepo = None, user_order_repo: UserOrderRepo = None,
                 user_profile_bundle_repo: UserProfileBundleRepo = None, user_repo: UserRepo = None,
                 user_wallet_service: UserWalletService = None, task_executor: TaskExecutor = None):
        self.__refund_repo = refund_repo or OrderRefundRepo()
        self.__user_order_repo = user_order_repo or UserOrderRepo()
        self.__user_profile_bundle_repo = user_profile_bundle_repo or UserProfileBundleRepo()
        self.__user_repo = user_repo or UserRepo()
        self.__user_wallet_service = user_wallet_service or UserWalletService()
        self.__task_executor = task_executor or TaskExecutor()

    # ------------------------------------------------------------------ entry points

    def handle_card_hub_failure(self, order: UserOrderModel, failure: Optional[HubOrderFailedException],
                                payment_intent_id: Optional[str], tax_transaction_id: Optional[str] = None,
                                error: Optional[BaseException] = None) -> RefundResult:
        """Stripe webhook (Assign / Topup, app and MCP card checkout): the Hub call failed after payment."""
        return self.__guarded(order, lambda: self.__handle(
            order, PaymentTypeEnum.CARD, failure, error,
            {"payment_intent_id": payment_intent_id, "tax_transaction_id": tax_transaction_id}))

    def handle_wallet_hub_failure(self, order: UserOrderModel, failure: Optional[HubOrderFailedException],
                                  amount: float, currency: str,
                                  error: Optional[BaseException] = None) -> RefundResult:
        """Wallet purchase / top-up: `amount` is exactly what was debited, in the wallet's `currency`."""
        return self.__guarded(order, lambda: self.__handle(
            order, PaymentTypeEnum.WALLET, failure, error, {"amount": float(amount), "currency": currency}))

    def flag_hub_failure_for_review(self, order: UserOrderModel, payment_type: str,
                                    failure: Optional[HubOrderFailedException] = None,
                                    error: Optional[BaseException] = None) -> RefundResult:
        """DCB / LinkAja: no automatic refund yet; flag for manual review and alert."""
        return self.__guarded(order, lambda: self.__flag(
            order, payment_type, f"no automatic refund for {payment_type}; {self.__reason(failure, error)}"))

    def retry_failed_refund(self, order_id: str) -> RefundResult:
        """For support / admin: retry a refund left in status `failed` (Card only).

        Re-claims failed -> pending atomically, then repeats the Stripe call with the same
        idempotency key, so concurrent retries or a refund that already went through cannot pay twice.
        """
        row = self.__refund_repo.transition(order_id, OrderRefundStatusEnum.FAILED,
                                            {"status": OrderRefundStatusEnum.PENDING, "updated_at": _now()})
        if row is None:
            logger.warning(f"[refund] order {order_id}: nothing to retry (no refund in status 'failed')")
            return RefundResult.ALREADY_HANDLED
        logger.info(f"[refund] order {order_id}: retrying the {row.payment_type} refund")
        return self.__execute(row)

    def has_refund_record(self, order_id: str) -> bool:
        """True when the order already has a refund / manual review row. On a database error the
        answer is True: the caller then skips re-processing rather than risk provisioning or paying
        twice for an order that may already be refunded."""
        try:
            return self.__refund_repo.get_by_order_id(order_id) is not None
        except Exception as e:
            logger.error(f"[refund] order {order_id}: could not read user_order_refund ({e}); "
                         f"treating the order as already handled")
            return True

    # ------------------------------------------------------------------ decision

    @staticmethod
    def __guarded(order: UserOrderModel, action: Callable[[], RefundResult]) -> RefundResult:
        try:
            return action()
        except Exception as e:
            logger.error(f"[refund] order {getattr(order, 'id', None)}: refund handling failed, "
                         f"manual follow-up required: {e}")
            return RefundResult.MANUAL_REVIEW

    def __handle(self, order: UserOrderModel, payment_type: str, failure: Optional[HubOrderFailedException],
                 error: Optional[BaseException], details: dict) -> RefundResult:
        reason = self.__reason(failure, error)
        if failure is None or not failure.definite:
            return self.__flag(order, payment_type, f"ambiguous eSIM Hub failure, not refunded: {reason}")
        provisioned = self.__provisioning_found(order.id)
        if provisioned:
            return self.__flag(order, payment_type, f"eSIM Hub reported a failure but {provisioned}; not refunded")

        row = self.__refund_repo.insert_if_absent({
            "order_id": order.id,
            "user_id": order.user_id,
            "payment_type": payment_type,
            "status": OrderRefundStatusEnum.PENDING,
            "reason": reason,
            **{k: v for k, v in details.items() if v is not None},
        })
        if row is None:
            logger.warning(f"[refund] order {order.id}: already claimed by an earlier attempt, nothing done")
            return RefundResult.ALREADY_HANDLED
        logger.info(f"[refund] order {order.id}: definite eSIM Hub failure, refunding ({payment_type})")
        return self.__execute(row)

    def __provisioning_found(self, order_id: str) -> Optional[str]:
        """Why the order must not be refunded automatically, or None. Read fresh from the database."""
        try:
            current = self.__user_order_repo.get_by_id(order_id)
            if current is None:
                return "the order could not be read"
            if current.esim_order_id:
                return f"the order has eSIM Hub order {current.esim_order_id}"
            if self.__user_profile_bundle_repo.get_first_by({"user_order_id": order_id}):
                return "a bundle was provisioned for the order"
            return None
        except Exception as e:
            return f"provisioning could not be checked ({e})"

    @staticmethod
    def __reason(failure: Optional[HubOrderFailedException], error: Optional[BaseException]) -> str:
        if failure is not None:
            return failure.reason
        if error is not None:
            return f"fulfilment raised after payment: {type(error).__name__}: {error}"
        return "unknown failure"

    # ------------------------------------------------------------------ execution

    def __execute(self, row: UserOrderRefundModel) -> RefundResult:
        if row.payment_type == PaymentTypeEnum.CARD:
            return self.__refund_card(row)
        if row.payment_type == PaymentTypeEnum.WALLET:
            return self.__refund_wallet(row)
        return self.__to_manual_review(row, f"no automatic refund for payment type {row.payment_type}")

    def __refund_card(self, row: UserOrderRefundModel) -> RefundResult:
        if not row.payment_intent_id:
            return self.__to_failed(row, "no Stripe PaymentIntent id for the order")
        try:
            # No `amount`: Stripe refunds the full captured amount (after promo, including tax).
            refund = stripe.Refund.create(
                payment_intent=row.payment_intent_id,
                reason="requested_by_customer",
                metadata={"order_id": row.order_id, "cause": "esim_hub_failure"},
                idempotency_key=f"{STRIPE_REFUND_IDEMPOTENCY_PREFIX}{row.order_id}",
            )
            amount = (getattr(refund, "amount", 0) or 0) / 100
            currency = (getattr(refund, "currency", "") or "").upper()
            reference = getattr(refund, "id", None)
        except stripe.error.InvalidRequestError as e:
            if getattr(e, "code", None) != "charge_already_refunded":
                return self.__to_failed(row, f"Stripe refund failed: {e}")
            logger.warning(f"[refund] order {row.order_id}: Stripe says the charge was already refunded")
            amount, currency, reference = None, None, "already refunded in Stripe"
        except Exception as e:
            return self.__to_failed(row, f"Stripe refund failed: {e}")

        refunded = self.__mark_refunded(row, amount, currency, reference)
        self.__reverse_tax(refunded or row)
        return RefundResult.REFUNDED

    def __reverse_tax(self, row: UserOrderRefundModel) -> None:
        if not row.tax_transaction_id or row.tax_reversal_id:
            return
        try:
            reversal = stripe.tax.Transaction.create_reversal(
                mode="full",
                original_transaction=row.tax_transaction_id,
                reference=f"{STRIPE_REFUND_IDEMPOTENCY_PREFIX}{row.order_id}",
            )
            self.__refund_repo.transition(row.order_id, OrderRefundStatusEnum.REFUNDED,
                                          {"tax_reversal_id": getattr(reversal, "id", None), "updated_at": _now()})
        except Exception as e:
            logger.error(f"[refund] order {row.order_id}: payment refunded but the Stripe Tax transaction "
                         f"{row.tax_transaction_id} was not reversed: {e}")
            self.__alert_admin("Stripe Tax reversal needed", row,
                               f"The payment was refunded, but Stripe Tax transaction {row.tax_transaction_id} "
                               f"could not be reversed: {e}")

    def __refund_wallet(self, row: UserOrderRefundModel) -> RefundResult:
        if row.amount is None or row.amount <= 0:
            return self.__to_manual_review(row, "no debited amount recorded for the wallet refund")
        try:
            # order_currency is set exactly as on the debit, so no FX is applied: the same amount
            # goes back in the wallet's currency.
            self.__user_wallet_service.add_wallet_transaction(
                amount=float(row.amount), user_id=row.user_id, source=UserWalletTransactionSource.REFUND_BUNDLE,
                order_currency="USD", send_push=False)
        except Exception as e:
            # The credit may or may not have been applied: never retried automatically.
            return self.__to_manual_review(row, f"wallet credit back failed, check the balance before "
                                                f"refunding by hand: {e}")
        self.__mark_refunded(row, row.amount, row.currency, "wallet credit")
        return RefundResult.REFUNDED

    # ------------------------------------------------------------------ state changes

    def __mark_refunded(self, row: UserOrderRefundModel, amount, currency, reference) -> Optional[UserOrderRefundModel]:
        now = _now()
        data = {"status": OrderRefundStatusEnum.REFUNDED, "reference": reference, "refunded_at": now,
                "updated_at": now}
        if amount is not None:
            data["amount"] = amount
        if currency:
            data["currency"] = currency
        try:
            updated = self.__refund_repo.transition(row.order_id, OrderRefundStatusEnum.PENDING, data)
        except Exception as e:
            updated = None
            logger.error(f"[refund] order {row.order_id}: refunded but the status could not be saved: {e}")
        if updated is None:
            # The money moved. The row stays 'pending', which no retry picks up, so it cannot be paid twice.
            self.__alert_admin("Refund done but not recorded", row,
                               f"The {row.payment_type} refund succeeded (reference {reference}) but "
                               f"user_order_refund could not be set to 'refunded'. Do not refund again.")
        logger.info(f"[refund] order {row.order_id}: refunded {amount} {currency} ({row.payment_type}, {reference})")
        self.__notify_customer(updated or row, amount, currency)
        return updated

    def __to_failed(self, row: UserOrderRefundModel, reason: str) -> RefundResult:
        logger.error(f"[refund] order {row.order_id}: {reason}")
        self.__refund_repo.transition(row.order_id, OrderRefundStatusEnum.PENDING,
                                      {"status": OrderRefundStatusEnum.FAILED, "reason": reason, "updated_at": _now()})
        self.__alert_admin("Automatic refund failed", row,
                           f"{reason}. Safe to retry: OrderRefundService.retry_failed_refund('{row.order_id}').")
        return RefundResult.FAILED

    def __to_manual_review(self, row: UserOrderRefundModel, reason: str) -> RefundResult:
        logger.error(f"[refund] order {row.order_id}: manual review needed: {reason}")
        self.__refund_repo.transition(row.order_id, OrderRefundStatusEnum.PENDING,
                                      {"status": OrderRefundStatusEnum.MANUAL_REVIEW, "reason": reason,
                                       "updated_at": _now()})
        self.__alert_admin("Order needs manual review", row, reason)
        return RefundResult.MANUAL_REVIEW

    def __flag(self, order: UserOrderModel, payment_type: str, reason: str) -> RefundResult:
        logger.error(f"[refund] order {order.id} ({payment_type}): manual review needed: {reason}")
        row = self.__refund_repo.insert_if_absent({
            "order_id": order.id,
            "user_id": order.user_id,
            "payment_type": payment_type,
            "status": OrderRefundStatusEnum.MANUAL_REVIEW,
            "reason": reason,
            "payment_intent_id": order.payment_intent_code,
        })
        if row is None:
            logger.warning(f"[refund] order {order.id}: already has a refund record, not flagged again")
            return RefundResult.ALREADY_HANDLED
        self.__alert_admin("Order needs manual review", row, reason)
        return RefundResult.MANUAL_REVIEW

    # ------------------------------------------------------------------ notifications

    def __notify_customer(self, row: UserOrderRefundModel, amount, currency) -> None:
        amount_display = f"{float(amount):.2f} {currency}" if amount is not None and currency else ""

        def task():
            try:
                fcm_service.send_notification_to_user_from_template(
                    send_order_refunded_notification(amount_display), user_id=row.user_id)
            except Exception as e:
                logger.error(f"[refund] order {row.order_id}: refund push failed: {e}")
            try:
                self.__send_customer_email(row, amount_display)
            except Exception as e:
                logger.error(f"[refund] order {row.order_id}: refund email failed: {e}")

        self.__task_executor.add_task(task)

    def __send_customer_email(self, row: UserOrderRefundModel, amount_display: str) -> None:
        user = self.__user_repo.get_by_id(record_id=row.user_id)
        if user is None:
            return
        metadata = user.metadata or {}
        recipient = metadata.get("email", user.email)
        if not recipient:
            return
        order = self.__user_order_repo.get_by_id(row.order_id)
        bundle_name = ""
        if order is not None and order.bundle_data:
            try:
                from app.schemas.bundle import BundleDTO
                bundle_name = BundleDTO.model_validate_json(order.bundle_data).bundle_name or ""
            except Exception:
                bundle_name = order.bundle_id or ""
        card = row.payment_type == PaymentTypeEnum.CARD
        data = {
            "order_id": row.order_id,
            "bundle_name": bundle_name,
            "amount": amount_display or "the full amount",
            "refunded_to": "Your card" if card else "Your wallet",
            "timing": ("Card refunds usually appear on your statement within 5 to 10 business days."
                       if card else "The amount is already available in your wallet."),
        }
        language = lower(metadata.get("language", "en") or "en")
        template = get_email_template(f"order_refunded_email_{language}.htm") \
            or get_email_template("order_refunded_email_en.htm")
        if template is None:
            logger.error("[refund] order refunded email template not found")
            return
        send_email(subject="Your payment was refunded", html_content=template.render(data=data),
                   recipients=recipient)

    def __alert_admin(self, headline: str, row: UserOrderRefundModel, detail: str) -> None:
        rows: Iterable[Tuple[str, str]] = [
            ("Order ID", row.order_id), ("User ID", row.user_id), ("Payment type", row.payment_type),
            ("Status", row.status), ("Payment intent", row.payment_intent_id or "-"),
            ("Amount", f"{row.amount} {row.currency}" if row.amount is not None else "-"),
            ("Detail", detail),
        ]

        def task():
            try:
                recipients = get_config("WALLET_TOP_UP_ALERT_RECIPIENTS")
                if not recipients:
                    logger.error(f"[refund] WALLET_TOP_UP_ALERT_RECIPIENTS is not configured, alert not emailed: "
                                 f"{headline} for order {row.order_id}")
                    return
                template = get_email_template("order_refund_admin_email_en.htm")
                if template is None:
                    logger.error("[refund] admin alert email template not found")
                    return
                send_email(subject=f"[eSIM] {headline}: order {row.order_id}",
                           html_content=template.render(data={"headline": headline, "rows": list(rows)}),
                           recipients=recipients)
            except Exception as e:
                logger.error(f"[refund] admin alert for order {row.order_id} failed: {e}")

        self.__task_executor.add_task(task)
