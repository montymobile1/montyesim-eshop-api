import os
import threading
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple

from fastapi import Request
from loguru import logger

from app.config.config import get_email_template, send_email
from app.config.constants import ErrorMessages, TopUpRefundStatus, TopUpReservationStatus, \
    UserWalletTransactionSource, UserWalletTransactionStatus
from app.config.db import OrderStatusEnum, UserOrderType
from app.config.helper import get_config
from app.config.notification_types import send_wallet_top_up_failed_notification, \
    send_wallet_top_up_succeeded_notification
from app.config.push_notification_manager import fcm_service
from app.config.settings import get_settings
from app.config.utils import create_wallet_top_up_intent, create_payment_ephemeral, parse_iso_datetime, \
    refund_payment_intent, stripe_get_payment_intent_status, truncate_two_decimals_decimal
from app.exceptions import CustomException
from app.models.user import UserWalletModel, UserModel, UserWalletTransactionModel, UsersCopyModel
from app.repo import UserWalletRepo, UserOrderRepo, UserWalletTransactionRepo, UserRepo, \
    UserWalletTopUpReservationRepo, UserWalletTopUpRefundRepo
from app.schemas.bundle import PaymentIntentResponse
from app.schemas.dto_mapper import DtoMapper
from app.schemas.response import Response, ResponseHelper
from app.schemas.user_wallet import DailyTopUpUsage, TopUpCreditResult, TopUpReservationResult, \
    UserWalletRequestDto, TopUpWalletRequest
from app.schemas.user_wallet import UserWalletResponse
from app.services.currency_service import CurrencyService

# the daily top-up limit is denominated in USD, whatever the system/wallet currency is
USD_CURRENCY = "USD"
# business validation errors are reported with 400 across the project (see UserOtpService)
DAILY_TOP_UP_LIMIT_ERROR_CODE = 400
# provider payment states a reservation can safely be released on: the payment is dead
RELEASABLE_PAYMENT_STATUSES = ("canceled",)
# stable per payment idempotency key, so a retried refund never becomes a second refund
REFUND_IDEMPOTENCY_PREFIX = "wallet-top-up-refund-"


class TopUpReservationOutcome:
    """Outcomes returned by the `reserve_wallet_top_up_daily_limit` database function."""
    RESERVED = "reserved"
    WALLET_NOT_FOUND = "wallet_not_found"
    COUNT_LIMIT_REACHED = "count_limit_reached"
    AMOUNT_LIMIT_EXCEEDED = "amount_limit_exceeded"


class TopUpCreditStatus:
    """Outcomes returned by the `complete_wallet_top_up_reservation` database function."""
    CREDITED = "credited"
    ALREADY_PROCESSED = "already_processed"
    REFUND_REQUIRED = "refund_required"
    WALLET_NOT_FOUND = "wallet_not_found"


def daily_top_up_window(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Return the [start, end) UTC bounds of the current day in the configured timezone.

    The window is recomputed on every call, so the daily usage resets on its own when a
    new day starts, without any scheduler or stored counter.
    """
    zone = get_settings().daily_top_up_limit_zone_info
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    start_of_day = datetime.combine(current.date(), time.min, tzinfo=zone)
    start_of_next_day = start_of_day + timedelta(days=1)
    return start_of_day.astimezone(timezone.utc), start_of_next_day.astimezone(timezone.utc)


class UserWalletService:
    def __init__(self):
        self.__user_wallet_repo = UserWalletRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_wallet_transaction_repo = UserWalletTransactionRepo()
        self.__currency_service = CurrencyService()
        self.__user_repo = UserRepo()
        self.__top_up_reservation_repo = UserWalletTopUpReservationRepo()
        self.__top_up_refund_repo = UserWalletTopUpRefundRepo()

    async def get_user_wallet_by_id(self, user_wallet_id: str) -> UserWalletResponse | None:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"id": user_wallet_id})
        if not wallet:
            return None
        return DtoMapper.to_user_wallet_response(wallet)

    async def create_wallet(self, user_wallet_request_dto: UserWalletRequestDto) -> UserWalletResponse:
        user_wallet = self.__user_wallet_repo.get_first_by(where={"user_id": user_wallet_request_dto.user_id})
        if user_wallet:
            return DtoMapper.to_user_wallet_response(user_wallet)
        wallet = self.__create_wallet(user_id=user_wallet_request_dto.user_id, amount=user_wallet_request_dto.amount)
        return DtoMapper.to_user_wallet_response(wallet)

    async def get_user_wallet_by_user_id(self, user_id: str, currency_code: str = os.getenv(
        "DEFAULT_CURRENCY")) -> UserWalletResponse | None:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"user_id": user_id})
        if not wallet:
            return None
        wallet.amount = self.__currency_service.convert(from_currency=os.getenv("SYSTEM_CURRENCY", "USD"),
                                                        to_currency=currency_code,
                                                        amount=wallet.amount)
        return DtoMapper.to_user_wallet_response(wallet)

    def get_user_wallet(self, user_id) -> UserWalletModel:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"user_id": user_id})
        return wallet

    def add_wallet_transaction(self, amount: float, user_id: str, source: str = "TopUp", order_currency: str = None,
                               payment_reference: str = None) -> \
            Response[
                UserWalletResponse]:
        try:
            user: UsersCopyModel = self.__user_repo.get_first_by(where={"id": user_id})
            transaction_currency = user.metadata.get("currency", os.getenv("SYSTEM_CURRENCY", "USD"))

            user_wallet: UserWalletModel = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
            if user_wallet is None:
                raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")

            transaction_amount = amount
            notification_amount = amount
            if user_wallet.currency != transaction_currency and order_currency is None:
                rate = self.__currency_service.get_currency_rate(from_currency=user_wallet.currency,
                                                                 to_currency=transaction_currency)
                transaction_amount = truncate_two_decimals_decimal(amount * rate)
            if user_wallet.currency != transaction_currency:
                rate = self.__currency_service.get_currency_rate(from_currency=user_wallet.currency,
                                                                 to_currency=transaction_currency)
                notification_amount = truncate_two_decimals_decimal(amount * rate)
            current_amount = float(user_wallet.amount)
            add_amount = float(transaction_amount)
            new_amount = current_amount + add_amount
            user_wallet.amount = new_amount
            self.__user_wallet_repo.update_by(where={"user_id": user_id},
                                              data=user_wallet.model_dump())

            transaction = self.__user_wallet_transaction_repo.create(data={
                "wallet_id": user_wallet.id,
                "amount": add_amount,
                "source": source,
                "status": "success"
            })
            self.__dispatch_transaction_notifications(user=user, user_wallet=user_wallet, transaction=transaction,
                                                      user_id=user_id, amount=amount,
                                                      notification_amount=notification_amount,
                                                      transaction_currency=transaction_currency, source=source,
                                                      payment_reference=payment_reference)
            dto = DtoMapper.to_user_wallet_response(user_wallet)
            return ResponseHelper.success_data_response(dto, 1)
        except Exception as e:
            logger.error(str(e))
            raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")

    def add_wallet_top_up_transaction(self, amount: float, user_id: str, order_currency: str = None,
                                      payment_reference: str = None, order_id: str = None) -> Response[
            UserWalletResponse]:
        """Credit a wallet top-up whose payment succeeded.

        With `DAILY_TOP_LIMIT` disabled the historical flow is used as is. With the flag
        enabled the credit goes through the database function that locks the wallet row,
        completes the daily limit reservation taken before the payment and stays idempotent
        per payment reference.
        """
        if not get_settings().daily_top_limit:
            return self.add_wallet_transaction(amount=amount, user_id=user_id,
                                               source=UserWalletTransactionSource.TOP_UP_WALLET,
                                               order_currency=order_currency, payment_reference=payment_reference)
        return self.__complete_top_up_reservation(amount=amount, user_id=user_id,
                                                  payment_reference=payment_reference, order_id=order_id)

    def release_top_up_reservation(self, order_id: str = None, payment_reference: str = None,
                                   status: str = TopUpReservationStatus.CANCELLED) -> None:
        """Give the daily capacity of a pending top-up back when its payment cannot succeed."""
        if not get_settings().daily_top_limit:
            return
        released = self.__top_up_reservation_repo.release(status=status, order_id=order_id,
                                                          payment_reference=payment_reference)
        if released:
            logger.info(f"released {len(released)} daily top-up reservation(s) for order {order_id} as {status}")

    def reconcile_stale_top_up_reservations(self, user_id: str) -> None:
        """Release the capacity of stale reservations, on the provider's word when there is one.

        A reservation that never got a payment intent attached is abandoned and expires on
        the configured TTL. One that already carries a payment intent is never released just
        because the local TTL passed: the provider is asked first and the capacity is only
        freed once the payment cannot be paid anymore. Anything still payable, already paid
        or unreadable keeps its capacity and is checked again later.
        """
        settings = get_settings()
        if not settings.daily_top_limit:
            return
        now = datetime.now(timezone.utc)
        for reservation in self.__top_up_reservation_repo.list_stale_pending(user_id=user_id, now=now):
            if not reservation.payment_reference:
                logger.info(f"expiring abandoned daily top-up reservation {reservation.id}: no payment was created")
                self.__top_up_reservation_repo.release(status=TopUpReservationStatus.EXPIRED,
                                                       reservation_id=reservation.id)
                continue
            payment_status = stripe_get_payment_intent_status(reservation.payment_reference)
            if payment_status in RELEASABLE_PAYMENT_STATUSES:
                logger.info(f"releasing daily top-up reservation {reservation.id}: payment is {payment_status}")
                self.__top_up_reservation_repo.release(status=TopUpReservationStatus.EXPIRED,
                                                       reservation_id=reservation.id)
                continue
            # still payable, already paid, or unknown: keep holding the capacity
            logger.info(f"keeping daily top-up reservation {reservation.id}: payment is {payment_status}")
            self.__top_up_reservation_repo.extend_expiry(
                reservation.id, now + timedelta(minutes=settings.daily_top_up_reservation_ttl_minutes))

    def retry_pending_top_up_refunds(self, limit: int = 50) -> int:
        """Retry the automatic refunds that did not reach the provider yet.

        Returns the number of refund records processed.
        """
        if not get_settings().daily_top_limit:
            return 0
        refunds = self.__top_up_refund_repo.list_retryable(limit=limit)
        for refund in refunds:
            self.__execute_top_up_refund(refund_id=refund.id, user_id=refund.user_id,
                                         payment_reference=refund.payment_reference, reason=refund.reason,
                                         attempt_count=refund.attempt_count)
        return len(refunds)

    def get_daily_top_up_usage(self, user_id: str) -> DailyTopUpUsage:
        """Completed top-ups and still active reservations of the user for the current day."""
        user_wallet: UserWalletModel = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
        if not user_wallet:
            return DailyTopUpUsage(currency=os.getenv("SYSTEM_CURRENCY", USD_CURRENCY))
        window_start, window_end = daily_top_up_window()
        usage = self.__user_wallet_transaction_repo.get_daily_top_up_usage(
            wallet_id=user_wallet.id, currency=user_wallet.currency,
            source=UserWalletTransactionSource.TOP_UP_WALLET, status=UserWalletTransactionStatus.SUCCESS,
            window_start=window_start, window_end=window_end)
        pending_count, pending_amount = self.__top_up_reservation_repo.get_active_daily_usage(
            wallet_id=user_wallet.id, window_start=window_start, window_end=window_end)
        usage.pending_reservation_count = pending_count
        usage.pending_reservation_amount = pending_amount
        return usage

    async def validate_daily_top_up_limits(self, user_id: str, requested_amount_usd: Decimal) -> None:
        """Reject a top-up that would break the configured daily count or amount limit.

        Pending reservations of unfinished payments hold capacity too, so concurrent
        payment intents cannot together exceed the limits. Returns immediately when
        `DAILY_TOP_LIMIT` is disabled.
        """
        settings = get_settings()
        if not settings.daily_top_limit:
            return
        usage = self.get_daily_top_up_usage(user_id=user_id)
        current_total_usd = self.__amount_in_usd(usage.reserved_amount, usage.currency)
        self.__assert_within_daily_limits(user_id=user_id, successful_count=usage.reserved_count,
                                          current_total_usd=current_total_usd,
                                          requested_amount_usd=requested_amount_usd)

    def reserve_daily_top_up(self, user_id: str, amount: float, order_id: str = None) -> Optional[str]:
        """Atomically validate the daily limits and hold the capacity of a pending top-up.

        Returns the reservation id, or None when the feature flag is disabled. Raises the
        matching limit exception when the request does not fit in the remaining capacity.
        """
        settings = get_settings()
        if not settings.daily_top_limit:
            return None
        user_wallet: UserWalletModel = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
        if user_wallet is None:
            raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")

        window_start, window_end = daily_top_up_window()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.daily_top_up_reservation_ttl_minutes)
        requested_amount = Decimal(str(amount))
        result: TopUpReservationResult = self.__user_wallet_repo.reserve_daily_top_up(
            user_id=user_id, amount=requested_amount, currency=user_wallet.currency,
            source=UserWalletTransactionSource.TOP_UP_WALLET, success_status=UserWalletTransactionStatus.SUCCESS,
            window_start=window_start, window_end=window_end, expires_at=expires_at,
            max_count=settings.daily_top_up_max_count,
            max_amount=self.__amount_from_usd(settings.daily_top_up_max_amount_usd, user_wallet.currency),
            order_id=order_id)

        if result.status == TopUpReservationOutcome.WALLET_NOT_FOUND:
            raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")
        if result.status == TopUpReservationOutcome.COUNT_LIMIT_REACHED:
            raise self.__count_limit_exception(user_id=user_id, successful_count=result.reserved_count)
        if result.status == TopUpReservationOutcome.AMOUNT_LIMIT_EXCEEDED:
            raise self.__amount_limit_exception(
                user_id=user_id,
                projected_total_usd=self.__amount_in_usd(result.reserved_amount + requested_amount,
                                                         user_wallet.currency))
        logger.info(f"daily top-up capacity reserved for user {user_id}: "
                    f"{result.reserved_count + 1}/{settings.daily_top_up_max_count} slots used")
        return result.reservation_id

    def attach_payment_reference_to_reservation(self, reservation_id: str, payment_reference: str) -> None:
        """Link a reservation to the provider payment once the payment intent exists."""
        if not reservation_id:
            return
        self.__top_up_reservation_repo.update(reservation_id, {"payment_reference": payment_reference})

    async def top_up_wallet(self, top_up_request: TopUpWalletRequest, user: UserModel, request: Request,
                            x_currency: str) -> Response[
        PaymentIntentResponse]:
        amount = top_up_request.amount

        user_wallet = self.__user_wallet_repo.get_first_by(where={"user_id": user.id})
        if not user_wallet:
            user_wallet = self.__create_wallet(user_id=user.user_id, amount=0)

        order_amount = amount
        if x_currency != user_wallet.currency:
            order_amount = self.__currency_service.convert(from_currency=x_currency,
                                                           to_currency=os.getenv("SYSTEM_CURRENCY", "USD"),
                                                           amount=top_up_request.amount)

        if order_amount <= 0.5:
            raise CustomException(code=400, name=ErrorMessages.INVALID_TOP_UP_AMOUNT,
                                  details="Top up amount must be greater than 0.5")
        # free the capacity of the user's abandoned payments before measuring what is left
        self.reconcile_stale_top_up_reservations(user_id=user.id)
        # validate the daily limits before creating the order and the payment intent
        await self.validate_daily_top_up_limits(
            user_id=user.id,
            requested_amount_usd=self.__amount_in_usd(Decimal(str(order_amount)),
                                                      os.getenv("SYSTEM_CURRENCY", USD_CURRENCY)))
        order = self.__user_order_repo.create(data={
            "user_id": user.id,
            "bundle_id": None,
            "order_type": UserOrderType.WALLET_TOP_UP,
            "amount": order_amount,
            "currency": os.getenv("SYSTEM_CURRENCY", "USD"),
            "bundle_data": "-",
            "searched_countries": "-",
            "anonymous_user_id": None,
        })

        # hold the daily capacity before the customer can be charged, so two concurrent
        # payment intents can never add up to more than the configured daily limits
        reservation_id = self.__reserve_daily_top_up_for_order(user_id=user.id, amount=order_amount, order=order)

        try:
            intent, tax = create_wallet_top_up_intent(user_email=user.email, amount=int(amount * 100),
                                                      currency=x_currency,
                                                      metadata={
                                                          "user_id": user.id,
                                                          "user_wallet_id": user_wallet.id,
                                                          "order_id": order.id,
                                                          "env": os.environ.get("ENVIRONMENT", "DEV"),
                                                      }, ip_address=request.client.host)
        except Exception:
            # no payment can happen anymore, give the reserved capacity back immediately
            self.release_top_up_reservation(order_id=order.id)
            raise
        tax_excl = round(float(getattr(tax, "tax_amount_exclusive", 0) / 100), 2)

        order.payment_intent_code = intent.id
        self.__user_order_repo.update_by({"id": order.id}, data=order.model_dump(exclude={"id"}))
        self.attach_payment_reference_to_reservation(reservation_id=reservation_id, payment_reference=intent.id)
        ephemeral = create_payment_ephemeral(intent.customer)
        response = PaymentIntentResponse(publishable_key=os.getenv("STRIPE_PUBLIC_KEY"),
                                         merchant_identifier=os.getenv("MERCHANT_ID"),
                                         payment_intent_client_secret=intent.client_secret,
                                         customer_id=intent.customer,
                                         customer_ephemeral_key_secret=ephemeral.secret,
                                         test_env=not intent.livemode,
                                         merchant_display_name=os.getenv("MERCHANT_DISPLAY_NAME"),
                                         billing_country_code="GB",
                                         order_id=order.id,
                                         total_price_display=f"{top_up_request.amount:.2f} {x_currency}",
                                         subtotal_price_display=f"{intent.amount / 100:.2f} {x_currency}",
                                         tax_price_display=f"{tax_excl} {x_currency}",
                                         has_tax=tax_excl > 0
                                         )
        return ResponseHelper.success_data_response(response, 0)

    def get_wallet_transactions(self, user_id: str) -> List[UserWalletTransactionModel]:
        user_wallet = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
        if not user_wallet:
            return []
        transactions = self.__user_wallet_transaction_repo.list(where={"wallet_id": user_wallet.id},
                                                                order_by="created_at", desc=True)
        return transactions

    def __reserve_daily_top_up_for_order(self, user_id: str, amount: float, order) -> Optional[str]:
        """Reserve the daily capacity of an order, marking it failed when it does not fit."""
        try:
            return self.reserve_daily_top_up(user_id=user_id, amount=amount, order_id=order.id)
        except CustomException:
            self.__user_order_repo.update(order.id, {"payment_status": OrderStatusEnum.FAILURE})
            raise

    def __complete_top_up_reservation(self, amount: float, user_id: str, payment_reference: str = None,
                                      order_id: str = None) -> Response[UserWalletResponse]:
        user: UsersCopyModel = self.__user_repo.get_first_by(where={"id": user_id})
        user_wallet: UserWalletModel = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
        if user_wallet is None:
            raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")

        settings = get_settings()
        window_start, window_end = daily_top_up_window()
        credit_amount = Decimal(str(amount))
        result: TopUpCreditResult = self.__user_wallet_repo.complete_top_up_reservation(
            user_id=user_id, amount=credit_amount, source=UserWalletTransactionSource.TOP_UP_WALLET,
            success_status=UserWalletTransactionStatus.SUCCESS, payment_reference=payment_reference,
            window_start=window_start, window_end=window_end, max_count=settings.daily_top_up_max_count,
            max_amount=self.__amount_from_usd(settings.daily_top_up_max_amount_usd, user_wallet.currency),
            order_id=order_id)

        if result.status == TopUpCreditStatus.WALLET_NOT_FOUND:
            raise CustomException(code=400, name=ErrorMessages.WALLET_NOT_FOUND, details="user wallet not found")
        if result.status == TopUpCreditStatus.REFUND_REQUIRED:
            # the payment succeeded but its capacity is gone (reservation expired, cancelled or
            # missing) and the daily limits are used up: the wallet is not credited and the
            # refund record written with that decision drives the automatic refund
            self.__refund_top_up_payment(result=result, user_id=user_id, payment_reference=payment_reference,
                                         order_id=order_id)
            user_wallet.amount = float(result.balance) if result.balance is not None else user_wallet.amount
            return ResponseHelper.success_data_response(DtoMapper.to_user_wallet_response(user_wallet), 1)
        if result.status == TopUpCreditStatus.ALREADY_PROCESSED:
            logger.info(f"wallet top-up already credited for user {user_id}, skipping duplicate callback")
            user_wallet.amount = float(result.balance) if result.balance is not None else user_wallet.amount
            return ResponseHelper.success_data_response(DtoMapper.to_user_wallet_response(user_wallet), 1)

        user_wallet.amount = float(result.balance)
        metadata = (user.metadata or {}) if user else {}
        transaction_currency = metadata.get("currency", os.getenv("SYSTEM_CURRENCY", USD_CURRENCY))
        notification_amount = amount
        if user_wallet.currency != transaction_currency:
            rate = self.__currency_service.get_currency_rate(from_currency=user_wallet.currency,
                                                             to_currency=transaction_currency)
            notification_amount = truncate_two_decimals_decimal(amount * rate)
        transaction = UserWalletTransactionModel(id=result.transaction_id, wallet_id=user_wallet.id, amount=amount,
                                                 status=UserWalletTransactionStatus.SUCCESS,
                                                 source=UserWalletTransactionSource.TOP_UP_WALLET,
                                                 payment_reference=payment_reference,
                                                 created_at=datetime.now(timezone.utc).isoformat())
        self.__dispatch_transaction_notifications(user=user, user_wallet=user_wallet, transaction=transaction,
                                                  user_id=user_id, amount=amount,
                                                  notification_amount=notification_amount,
                                                  transaction_currency=transaction_currency,
                                                  source=UserWalletTransactionSource.TOP_UP_WALLET,
                                                  payment_reference=payment_reference)
        logger.info(f"wallet top-up credited for user {user_id} (reservation {result.reservation_id})")
        return ResponseHelper.success_data_response(DtoMapper.to_user_wallet_response(user_wallet), 1)

    def __refund_top_up_payment(self, result: TopUpCreditResult, user_id: str, payment_reference: str = None,
                                order_id: str = None) -> None:
        """Refund a paid top-up that cannot be credited, at most once per payment."""
        if result.refund_status == TopUpRefundStatus.SUCCEEDED:
            logger.info(f"top-up payment of order {order_id} was already refunded, nothing to do")
            return
        logger.warning(f"wallet top-up of user {user_id} (order {order_id}) cannot be credited "
                       f"({result.refund_reason}), refunding the payment")
        self.__execute_top_up_refund(refund_id=result.refund_id, user_id=user_id,
                                     payment_reference=payment_reference, reason=result.refund_reason,
                                     attempt_count=result.attempt_count)
        # the top-up did not go through, tell the user like any other failed top-up
        try:
            fcm_service.send_notification_to_user_from_template(send_wallet_top_up_failed_notification(),
                                                                user_id=user_id)
        except Exception as e:
            logger.error(f"error while notifying the user of a refunded top-up: {str(e)}")

    def __execute_top_up_refund(self, refund_id: str, user_id: str, payment_reference: str, reason: str,
                                attempt_count: int) -> None:
        """Ask the provider for the refund and persist the outcome of the attempt.

        Failures leave the record in a retryable state instead of raising, so the payment is
        never silently dropped.
        """
        attempt = (attempt_count or 0) + 1
        if not payment_reference:
            logger.error(f"top-up refund {refund_id} of user {user_id} has no provider payment reference, "
                         f"manual reconciliation required")
            self.__top_up_refund_repo.mark_failed(refund_id, error="missing provider payment reference",
                                                  attempt_count=attempt)
            return
        try:
            refund = refund_payment_intent(payment_intent_id=payment_reference,
                                           idempotency_key=f"{REFUND_IDEMPOTENCY_PREFIX}{payment_reference}",
                                           reason=reason)
            self.__top_up_refund_repo.mark_succeeded(refund_id, provider_refund_reference=getattr(refund, "id", None),
                                                     attempt_count=attempt)
            logger.info(f"top-up payment of user {user_id} refunded on attempt {attempt} ({reason})")
        except Exception as e:
            self.__top_up_refund_repo.mark_failed(refund_id, error=str(e), attempt_count=attempt)
            logger.error(f"top-up refund attempt {attempt} failed for user {user_id} ({reason}): {str(e)}")

    def __assert_within_daily_limits(self, user_id: str, successful_count: int, current_total_usd: Decimal,
                                     requested_amount_usd: Decimal) -> None:
        settings = get_settings()
        if successful_count >= settings.daily_top_up_max_count:
            raise self.__count_limit_exception(user_id=user_id, successful_count=successful_count)
        projected_total = current_total_usd + requested_amount_usd
        if projected_total > settings.daily_top_up_max_amount_usd:
            raise self.__amount_limit_exception(user_id=user_id, projected_total_usd=projected_total)

    @staticmethod
    def __count_limit_exception(user_id: str, successful_count: int) -> CustomException:
        settings = get_settings()
        logger.info(f"daily top-up count limit reached for user {user_id}: "
                    f"{successful_count}/{settings.daily_top_up_max_count}")
        return CustomException(code=DAILY_TOP_UP_LIMIT_ERROR_CODE,
                               name=ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED,
                               details=f"maximum of {settings.daily_top_up_max_count} successful top-ups per day "
                                       f"already reached")

    @staticmethod
    def __amount_limit_exception(user_id: str, projected_total_usd: Decimal) -> CustomException:
        settings = get_settings()
        logger.info(f"daily top-up amount limit exceeded for user {user_id}: projected total "
                    f"{projected_total_usd} USD over {settings.daily_top_up_max_amount_usd} USD")
        return CustomException(code=DAILY_TOP_UP_LIMIT_ERROR_CODE,
                               name=ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED,
                               details=f"maximum daily top-up amount of USD "
                                       f"{settings.daily_top_up_max_amount_usd_display} would be exceeded",
                               params={"amount": settings.daily_top_up_max_amount_usd_display})

    def __amount_in_usd(self, amount: Decimal, currency: str) -> Decimal:
        if currency == USD_CURRENCY:
            return amount
        converted = self.__currency_service.convert(from_currency=currency, to_currency=USD_CURRENCY,
                                                    amount=float(amount))
        return self.__to_currency_precision(converted)

    def __amount_from_usd(self, amount_usd: Decimal, currency: str) -> Decimal:
        if currency == USD_CURRENCY:
            return amount_usd
        converted = self.__currency_service.convert(from_currency=USD_CURRENCY, to_currency=currency,
                                                    amount=float(amount_usd))
        return self.__to_currency_precision(converted)

    @staticmethod
    def __to_currency_precision(amount: float) -> Decimal:
        """Round a converted amount to the currency precision, the exchange rate is a float."""
        return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def __dispatch_transaction_notifications(self, user: UsersCopyModel, user_wallet: UserWalletModel,
                                             transaction: UserWalletTransactionModel, user_id: str, amount: float,
                                             notification_amount: float, transaction_currency: str, source: str,
                                             payment_reference: str = None):
        if amount > 0:
            thread = threading.Thread(target=self.__send_push,
                                      args=(notification_amount, transaction_currency, user_id))
            thread.start()
        try:
            send_notification = os.getenv("SEND_WALLET_TOPUP_NOTIFICATION", "false").lower() in (
                "true", "1", "yes")
            if send_notification and source == UserWalletTransactionSource.TOP_UP_WALLET and transaction is not None:
                email_thread = threading.Thread(target=self.__send_top_up_admin_email,
                                                args=(user, user_wallet, transaction, payment_reference))
                email_thread.start()
        except Exception as e:
            logger.error(f"error while dispatching wallet top-up admin email: {str(e)}")

    def __create_wallet(self, user_id: str, amount: float):
        wallet = self.__user_wallet_repo.create(data={
            "user_id": user_id,
            "amount": amount,
            "currency": os.getenv("SYSTEM_CURRENCY", "USD")
        })
        logger.info(f"creating wallet for user {user_id}")
        return wallet

    def __send_push(self, amount: float, currency: str, user_id: str):
        content = send_wallet_top_up_succeeded_notification(f"{amount} {currency}")
        fcm_service.send_notification_to_user_from_template(content, user_id=user_id)

    def __send_top_up_admin_email(self, user: UsersCopyModel, user_wallet: UserWalletModel,
                                  transaction: UserWalletTransactionModel, payment_reference: str = None):
        try:
            day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
            transactions = self.__user_wallet_transaction_repo.list_since(where={"status": "success"},
                                                                          since=day_start)
            top_ups = [t for t in transactions if t.source == UserWalletTransactionSource.TOP_UP_WALLET]
            if transaction.id not in {t.id for t in top_ups}:
                top_ups.append(transaction)

            wallet_ids = list({t.wallet_id for t in top_ups})
            wallets = self.__user_wallet_repo.list_in(where={}, filter={"id": wallet_ids})
            running_balances = {w.id: float(w.amount) for w in wallets}

            balance_after = {}
            for t in sorted(transactions, key=lambda x: x.created_at or "", reverse=True):
                if t.wallet_id in running_balances:
                    balance_after[t.id] = running_balances[t.wallet_id]
                    running_balances[t.wallet_id] -= float(t.amount)

            today_top_ups = []
            for t in top_ups:
                created_at = parse_iso_datetime(t.created_at)
                today_top_ups.append({
                    "time": created_at.strftime("%H:%M:%S") if created_at else "-",
                    "transaction_id": t.id,
                    "amount": f"{float(t.amount):.2f}",
                    "balance_after": f"{balance_after.get(t.id, float(user_wallet.amount)):.2f}",
                })

            transaction_time = parse_iso_datetime(transaction.created_at) or datetime.now(timezone.utc)
            metadata = user.metadata or {}
            data = {
                "user_email": metadata.get("email", user.email),
                "currency": user_wallet.currency,
                "top_up_amount": f"{float(transaction.amount):.2f}",
                "transaction_id": payment_reference or transaction.id,
                "top_up_datetime": transaction_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "current_balance": f"{float(user_wallet.amount):.2f}",
                "top_up_count_today": len(top_ups),
                "total_top_up_amount_today": f"{sum(float(t.amount) for t in top_ups):.2f}",
                "today_top_ups": today_top_ups,
            }
            recipients = get_config("WALLET_TOP_UP_ALERT_RECIPIENTS")
            if not recipients:
                logger.error("WALLET_TOP_UP_ALERT_RECIPIENTS is not configured, skipping top-up admin email")
                return
            template = get_email_template("wallet_top_up_admin_email_en.htm")
            if template is None:
                logger.error("wallet top-up admin email template not found")
                return
            html_content = template.render(data=data)
            send_email(subject="New Wallet Top-Up Completed", html_content=html_content, recipients=recipients)
        except Exception as e:
            logger.error(f"error while sending wallet top-up admin email: {str(e)}")
