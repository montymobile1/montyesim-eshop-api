"""Integration tests for the daily wallet top-up limits.

Only the external providers are faked (Supabase driver, Stripe, push notifications):
the repositories, the daily aggregation and the limit rules are the real ones.
"""
import asyncio
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.constants import ErrorMessages, TopUpRefundReason, TopUpRefundStatus, TopUpReservationStatus, \
    UserWalletTransactionSource, UserWalletTransactionStatus
from app.exceptions import CustomException
from app.models.user import UserModel
from app.services.callback_service import CallbackService
from app.services.user_wallet_service import UserWalletService
from tests.services.fake_supabase import FakeSupabaseClient
from tests.services.test_daily_top_up_limit_service import daily_limit_settings

USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


@pytest.fixture
def database():
    return FakeSupabaseClient()


@pytest.fixture
def wallet_service(database):
    with patch('app.repo.base_repo.supabase_client', return_value=database):
        service = UserWalletService()
    return service


def seed_wallet(database, user_id: str = USER_ID, currency: str = "USD", amount: float = 0.0) -> dict:
    database.seed("users_copy", {"id": user_id, "email": "user@example.com", "metadata": {"currency": currency}})
    return database.seed("user_wallet", {"user_id": user_id, "amount": amount, "currency": currency})


def seed_transaction(database, wallet: dict, amount: float, created_at: datetime = None,
                     status: str = UserWalletTransactionStatus.SUCCESS,
                     source: str = UserWalletTransactionSource.TOP_UP_WALLET,
                     payment_reference: str = None) -> dict:
    return database.seed("user_wallet_transaction", {
        "wallet_id": wallet["id"],
        "amount": amount,
        "status": status,
        "source": source,
        "payment_reference": payment_reference,
        "created_at": utc_iso(created_at or datetime.now(timezone.utc)),
    })


def validate(service, amount: str, user_id: str = USER_ID):
    return asyncio.run(service.validate_daily_top_up_limits(user_id=user_id, requested_amount_usd=Decimal(amount)))


def seed_reservation(database, wallet: dict, amount: float, order_id: str = None, payment_reference: str = None,
                     status: str = TopUpReservationStatus.PENDING, expires_at: datetime = None,
                     created_at: datetime = None) -> dict:
    expires_at = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=30))
    return database.seed("user_wallet_top_up_reservation", {
        "user_id": wallet["user_id"],
        "wallet_id": wallet["id"],
        "order_id": order_id,
        "amount": amount,
        "currency": wallet["currency"],
        "status": status,
        "payment_reference": payment_reference,
        "transaction_id": None,
        "expires_at": utc_iso(expires_at),
        "created_at": utc_iso(created_at or datetime.now(timezone.utc)),
    })


def successful_top_ups(database, wallet: dict):
    return [row for row in database.rows("user_wallet_transaction")
            if row["wallet_id"] == wallet["id"]
            and row["status"] == UserWalletTransactionStatus.SUCCESS
            and row["source"] == UserWalletTransactionSource.TOP_UP_WALLET]


def reservations_of(database, wallet: dict):
    return [row for row in database.rows("user_wallet_top_up_reservation") if row["wallet_id"] == wallet["id"]]


def pending_reservations(database, wallet: dict):
    return [row for row in reservations_of(database, wallet) if row["status"] == TopUpReservationStatus.PENDING]


def refunds_of(database):
    return database.rows("user_wallet_top_up_refund")


@contextmanager
def stripe_refund(refund_reference: str = "re_1", error: Exception = None):
    """Fake the provider refund call, the only external part of the refund flow."""
    target = 'app.services.user_wallet_service.refund_payment_intent'
    if error is not None:
        with patch(target, side_effect=error) as refund_call:
            yield refund_call
    else:
        with patch(target, return_value=MagicMock(id=refund_reference)) as refund_call:
            yield refund_call


# --- daily usage aggregation ------------------------------------------------------

def test_first_top_up_of_the_day_is_allowed(database, wallet_service):
    seed_wallet(database)
    with daily_limit_settings():
        assert validate(wallet_service, '40') is None


def test_second_top_up_within_both_limits_is_allowed(database, wallet_service):
    wallet = seed_wallet(database, amount=40.0)
    seed_transaction(database, wallet, amount=40.0)
    with daily_limit_settings():
        assert validate(wallet_service, '50') is None
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
    assert usage.successful_topup_count == 1
    assert usage.successful_topup_amount == Decimal("40")


def test_third_top_up_of_the_day_is_rejected(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=10.0)
    seed_transaction(database, wallet, amount=15.0)
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '5')
    assert error.value.name == ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED


def test_top_up_over_the_daily_amount_is_rejected(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '30')
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED


def test_top_up_reaching_exactly_the_daily_amount_is_allowed(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=70.0)
    with daily_limit_settings():
        assert validate(wallet_service, '30') is None


def test_only_successful_top_ups_are_counted(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=50.0, status=UserWalletTransactionStatus.PENDING)
    seed_transaction(database, wallet, amount=50.0, status=UserWalletTransactionStatus.FAILED)
    seed_transaction(database, wallet, amount=50.0, status="cancelled")
    seed_transaction(database, wallet, amount=50.0, status="rejected")
    seed_transaction(database, wallet, amount=20.0, source=UserWalletTransactionSource.CASHBACK)
    seed_transaction(database, wallet, amount=20.0, source=UserWalletTransactionSource.VOUCHER)
    seed_transaction(database, wallet, amount=30.0)
    with daily_limit_settings():
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
        assert validate(wallet_service, '70') is None
    assert usage.successful_topup_count == 1
    assert usage.successful_topup_amount == Decimal("30")


def test_transactions_of_other_users_are_ignored(database, wallet_service):
    seed_wallet(database)
    other_wallet = seed_wallet(database, user_id=OTHER_USER_ID)
    seed_transaction(database, other_wallet, amount=60.0)
    seed_transaction(database, other_wallet, amount=40.0)
    with daily_limit_settings():
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
        assert validate(wallet_service, '100') is None
    assert usage.successful_topup_count == 0
    assert usage.successful_topup_amount == Decimal("0")


def test_limits_reset_on_a_new_day(database, wallet_service):
    wallet = seed_wallet(database)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    seed_transaction(database, wallet, amount=60.0, created_at=yesterday)
    seed_transaction(database, wallet, amount=40.0, created_at=yesterday)
    with daily_limit_settings():
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
        assert validate(wallet_service, '100') is None
    assert usage.successful_topup_count == 0


def test_transactions_around_midnight_belong_to_the_right_day(database, wallet_service):
    wallet = seed_wallet(database)
    # Beirut is UTC+3 in summer: the local day starts at 21:00 UTC the day before
    before_local_midnight = datetime(2026, 7, 30, 20, 59, tzinfo=timezone.utc)
    after_local_midnight = datetime(2026, 7, 30, 21, 1, tzinfo=timezone.utc)
    seed_transaction(database, wallet, amount=25.0, created_at=before_local_midnight)
    seed_transaction(database, wallet, amount=35.0, created_at=after_local_midnight)

    with daily_limit_settings(DAILY_TOP_UP_LIMIT_TIMEZONE='Asia/Beirut'):
        with patch('app.services.user_wallet_service.daily_top_up_window',
                   return_value=(datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc),
                                 datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc))):
            new_day = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
        with patch('app.services.user_wallet_service.daily_top_up_window',
                   return_value=(datetime(2026, 7, 29, 21, 0, tzinfo=timezone.utc),
                                 datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc))):
            previous_day = wallet_service.get_daily_top_up_usage(user_id=USER_ID)

    assert (previous_day.successful_topup_count, previous_day.successful_topup_amount) == (1, Decimal("25"))
    assert (new_day.successful_topup_count, new_day.successful_topup_amount) == (1, Decimal("35"))


def test_daily_total_is_compared_after_conversion_to_usd(database, wallet_service):
    wallet = seed_wallet(database, currency="EUR")
    seed_transaction(database, wallet, amount=45.0)
    seed_transaction(database, wallet, amount=45.0)
    # EUR 90 is USD 99 with this rate, so EUR 90 alone stays under the USD 100 limit
    database.seed("currency", {"id": 1, "name": "USD", "default_currency": "EUR", "rate": 1.1})
    with patch.dict('os.environ', {"SYSTEM_CURRENCY": "EUR"}):
        with daily_limit_settings(DAILY_TOP_UP_MAX_COUNT=5):
            assert validate(wallet_service, '1') is None
            with pytest.raises(CustomException) as error:
                validate(wallet_service, '1.5')
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED



# --- top-up request: reserve the daily capacity before charging -------------------

def top_up(wallet_service, amount: float, currency: str = "USD", intent_error: Exception = None):
    user = UserModel(id=USER_ID, email="user@example.com", token="t", msisdn=None, is_verified=True)
    request = MagicMock(client=MagicMock(host="127.0.0.1"))
    intent = MagicMock(id=f"pi_{uuid.uuid4().hex}", customer="cus_1", client_secret="secret", livemode=False,
                       amount=int(amount * 100))
    intent_mock = patch('app.services.user_wallet_service.create_wallet_top_up_intent',
                        side_effect=intent_error) if intent_error else patch(
        'app.services.user_wallet_service.create_wallet_top_up_intent', return_value=(intent, None))
    with intent_mock as create_intent, \
         patch('app.services.user_wallet_service.create_payment_ephemeral',
               return_value=MagicMock(secret="ephemeral")):
        response = asyncio.run(wallet_service.top_up_wallet(top_up_request=MagicMock(amount=amount), user=user,
                                                            request=request, x_currency=currency))
    return response, create_intent, intent


def test_top_up_request_over_the_amount_is_rejected_before_any_payment_is_created(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            top_up(wallet_service, amount=30.0)
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED
    assert pending_reservations(database, wallet) == []


def test_top_up_request_over_the_count_is_rejected_before_any_payment_is_created(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=5.0)
    seed_transaction(database, wallet, amount=5.0)
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            top_up(wallet_service, amount=10.0)
    assert error.value.name == ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED
    assert pending_reservations(database, wallet) == []


def test_request_rejected_before_its_order_exists_leaves_no_order_behind(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with pytest.raises(CustomException):
            top_up(wallet_service, amount=30.0)
    assert database.rows("user_order") == []


def test_top_up_request_within_the_limits_reserves_capacity_and_creates_the_payment_intent(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=70.0)
    with daily_limit_settings():
        response, create_intent, intent = top_up(wallet_service, amount=30.0)
    create_intent.assert_called_once()
    assert response.status == "success"
    reservations = pending_reservations(database, wallet)
    assert len(reservations) == 1
    assert reservations[0]["amount"] == 30.0
    assert reservations[0]["payment_reference"] == intent.id
    assert reservations[0]["order_id"] == database.rows("user_order")[0]["id"]


def test_pending_reservation_is_not_reported_as_a_successful_top_up(database, wallet_service):
    seed_wallet(database)
    with daily_limit_settings():
        top_up(wallet_service, amount=40.0)
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
    assert (usage.successful_topup_count, usage.successful_topup_amount) == (0, Decimal("0"))
    assert (usage.pending_reservation_count, usage.pending_reservation_amount) == (1, Decimal("40"))
    assert (usage.reserved_count, usage.reserved_amount) == (1, Decimal("40"))


def test_completed_reservation_and_its_transaction_are_counted_once(database, wallet_service):
    wallet = seed_wallet(database)
    with daily_limit_settings():
        # reserve, then settle the payment: the same top-up must not be counted twice
        top_up(wallet_service, amount=60.0)
        reservation = pending_reservations(database, wallet)[0]
        reserved_usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
        credit(wallet_service, amount=60.0, payment_reference=reservation["payment_reference"])
        settled_usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)

    assert (reserved_usage.successful_topup_count, reserved_usage.pending_reservation_count) == (0, 1)
    assert reserved_usage.reserved_amount == Decimal("60")
    assert (settled_usage.successful_topup_count, settled_usage.pending_reservation_count) == (1, 0)
    assert settled_usage.successful_topup_amount == Decimal("60")
    assert settled_usage.reserved_count == 1
    assert settled_usage.reserved_amount == Decimal("60")
    assert reservation["status"] == TopUpReservationStatus.COMPLETED


def test_pending_reservation_blocks_a_request_exceeding_the_daily_amount(database, wallet_service):
    seed_wallet(database)
    with daily_limit_settings():
        top_up(wallet_service, amount=60.0)
        with pytest.raises(CustomException) as error:
            top_up(wallet_service, amount=50.0)
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED


def test_pending_reservations_block_a_request_exceeding_the_daily_count(database, wallet_service):
    seed_wallet(database)
    with daily_limit_settings():
        top_up(wallet_service, amount=5.0)
        top_up(wallet_service, amount=5.0)
        with pytest.raises(CustomException) as error:
            top_up(wallet_service, amount=5.0)
    assert error.value.name == ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED


def test_abandoned_reservation_without_a_payment_expires_on_the_ttl(database, wallet_service):
    wallet = seed_wallet(database)
    abandoned = seed_reservation(database, wallet, amount=90.0,
                                 expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with daily_limit_settings():
        response, create_intent, _ = top_up(wallet_service, amount=90.0)
    assert response.status == "success"
    create_intent.assert_called_once()
    assert abandoned["status"] == TopUpReservationStatus.EXPIRED
    assert len(pending_reservations(database, wallet)) == 1


def test_reservation_of_an_active_payment_is_not_released_on_the_ttl(database, wallet_service):
    wallet = seed_wallet(database)
    stale = seed_reservation(database, wallet, amount=90.0, payment_reference="pi_processing",
                             expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with daily_limit_settings():
        # the provider still reports the payment as payable, so its capacity is kept
        with patch('app.services.user_wallet_service.stripe_get_payment_intent_status',
                   return_value="processing") as intent_status:
            with pytest.raises(CustomException) as error:
                top_up(wallet_service, amount=90.0)
    intent_status.assert_called_once_with("pi_processing")
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED
    assert stale["status"] == TopUpReservationStatus.PENDING
    # the reservation keeps holding the capacity and is checked again later
    assert utc_iso(datetime.now(timezone.utc)) < stale["expires_at"]


def test_reservation_of_a_cancelled_payment_is_released(database, wallet_service):
    wallet = seed_wallet(database)
    stale = seed_reservation(database, wallet, amount=90.0, payment_reference="pi_abandoned",
                             expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with daily_limit_settings():
        with patch('app.services.user_wallet_service.stripe_get_payment_intent_status',
                   return_value="canceled"):
            response, create_intent, _ = top_up(wallet_service, amount=90.0)
    assert response.status == "success"
    create_intent.assert_called_once()
    assert stale["status"] == TopUpReservationStatus.EXPIRED


def test_reservation_is_released_when_the_payment_intent_cannot_be_created(database, wallet_service):
    wallet = seed_wallet(database)
    with daily_limit_settings():
        with pytest.raises(RuntimeError):
            top_up(wallet_service, amount=40.0, intent_error=RuntimeError("stripe is down"))
        # the capacity is free again for the next attempt
        response, _, _ = top_up(wallet_service, amount=100.0)
    assert response.status == "success"
    assert [row["status"] for row in reservations_of(database, wallet)] == ["cancelled", "pending"]


def test_top_up_request_is_untouched_when_the_feature_flag_is_disabled(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=500.0)
    seed_transaction(database, wallet, amount=500.0)
    seed_transaction(database, wallet, amount=500.0)
    with daily_limit_settings(DAILY_TOP_LIMIT='false'):
        response, create_intent, _ = top_up(wallet_service, amount=300.0)
    assert response.status == "success"
    create_intent.assert_called_once()
    assert database.rows("user_wallet_top_up_reservation") == []
    assert database.rpc_calls == []


# --- concurrent requests ----------------------------------------------------------

def concurrent_top_ups(database, wallet_service, amounts: list) -> list:
    """Run the top-up requests in parallel and return their outcome, in call order."""
    outcomes = [None] * len(amounts)
    database.rpc_delay_seconds = 0.05

    def run(index: int):
        try:
            top_up(wallet_service, amount=amounts[index])
            outcomes[index] = "reserved"
        except CustomException as error:
            outcomes[index] = str(error.name)

    with ThreadPoolExecutor(max_workers=len(amounts)) as pool:
        list(pool.map(run, range(len(amounts))))
    return outcomes


def test_concurrent_requests_compete_for_the_last_count_slot(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=1.0)
    with daily_limit_settings():
        outcomes = concurrent_top_ups(database, wallet_service, [1.0, 1.0])
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
    assert sorted(outcomes) == sorted(["reserved", str(ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED)])
    assert usage.reserved_count == 2
    assert len(pending_reservations(database, wallet)) == 1


def skip_pre_validation():
    """Let a request reach the reservation without its pre-check, as in a real race."""
    return patch.object(UserWalletService, 'validate_daily_top_up_limits', new=AsyncMock(return_value=None))


def test_reservation_rejects_a_request_that_passed_the_pre_check(database, wallet_service):
    wallet = seed_wallet(database)
    seed_reservation(database, wallet, amount=60.0)
    with daily_limit_settings():
        with skip_pre_validation():
            with pytest.raises(CustomException) as error:
                top_up(wallet_service, amount=60.0)
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED
    assert len(pending_reservations(database, wallet)) == 1
    # the order created for the rejected request is not left pending forever
    assert [order["payment_status"] for order in database.rows("user_order")] == ["failure"]


def test_concurrent_reservations_are_serialized_on_the_wallet(database, wallet_service):
    wallet = seed_wallet(database)
    with daily_limit_settings():
        with skip_pre_validation():
            outcomes = concurrent_top_ups(database, wallet_service, [60.0, 60.0])
    assert sorted(outcomes) == sorted(["reserved", str(ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED)])
    assert len(pending_reservations(database, wallet)) == 1


def test_concurrent_reservations_never_exceed_the_count_limit(database, wallet_service):
    wallet = seed_wallet(database)
    with daily_limit_settings(DAILY_TOP_UP_MAX_COUNT=1):
        with skip_pre_validation():
            outcomes = concurrent_top_ups(database, wallet_service, [10.0, 10.0])
    assert sorted(outcomes) == sorted(["reserved", str(ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED)])
    assert len(pending_reservations(database, wallet)) == 1


def test_concurrent_requests_compete_for_the_remaining_amount(database, wallet_service):
    wallet = seed_wallet(database)
    with daily_limit_settings():
        outcomes = concurrent_top_ups(database, wallet_service, [60.0, 60.0])
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
    assert sorted(outcomes) == sorted(["reserved", str(ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED)])
    assert usage.reserved_amount == Decimal("60")
    assert len(pending_reservations(database, wallet)) == 1


# --- successful payment callback --------------------------------------------------

def credit(wallet_service, amount: float, payment_reference: str = "pi_1", order_id: str = None):
    with patch('app.services.user_wallet_service.threading.Thread', MagicMock()):
        return wallet_service.add_wallet_top_up_transaction(amount=amount, user_id=USER_ID, order_currency="USD",
                                                            payment_reference=payment_reference, order_id=order_id)


def test_successful_callback_completes_the_reservation_and_credits_once(database, wallet_service):
    wallet = seed_wallet(database, amount=10.0)
    reservation = seed_reservation(database, wallet, amount=40.0, payment_reference="pi_1")
    with daily_limit_settings():
        credit(wallet_service, amount=40.0, payment_reference="pi_1")
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
    assert wallet["amount"] == 50.0
    assert reservation["status"] == "completed"
    assert reservation["transaction_id"] == successful_top_ups(database, wallet)[0]["id"]
    assert (usage.successful_topup_count, usage.pending_reservation_count) == (1, 0)


def test_two_successful_callbacks_keep_the_daily_totals(database, wallet_service):
    wallet = seed_wallet(database)
    seed_reservation(database, wallet, amount=40.0, payment_reference="pi_1")
    seed_reservation(database, wallet, amount=50.0, payment_reference="pi_2")
    with daily_limit_settings():
        credit(wallet_service, amount=40.0, payment_reference="pi_1")
        credit(wallet_service, amount=50.0, payment_reference="pi_2")
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
    assert (usage.successful_topup_count, usage.successful_topup_amount) == (2, Decimal("90"))
    assert wallet["amount"] == 90.0


def test_replayed_callback_does_not_credit_twice(database, wallet_service):
    wallet = seed_wallet(database)
    reservation = seed_reservation(database, wallet, amount=40.0, payment_reference="pi_duplicate")
    with daily_limit_settings():
        credit(wallet_service, amount=40.0, payment_reference="pi_duplicate")
        credit(wallet_service, amount=40.0, payment_reference="pi_duplicate")
        credit(wallet_service, amount=40.0, payment_reference="pi_duplicate")
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
    assert wallet["amount"] == 40.0
    assert usage.successful_topup_count == 1
    assert len(successful_top_ups(database, wallet)) == 1
    assert len([row for row in reservations_of(database, wallet) if row["status"] == "completed"]) == 1
    assert reservation["transaction_id"] is not None


def test_paid_top_up_still_within_the_limits_is_credited_without_its_reservation(database, wallet_service):
    wallet = seed_wallet(database)
    expired = seed_reservation(database, wallet, amount=30.0, payment_reference="pi_late",
                               expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                               status=TopUpReservationStatus.EXPIRED)
    with daily_limit_settings():
        credit(wallet_service, amount=30.0, payment_reference="pi_late")
    # the capacity was free again, so the payment is simply credited
    assert wallet["amount"] == 30.0
    assert expired["status"] == TopUpReservationStatus.COMPLETED
    assert refunds_of(database) == []


def test_paid_top_up_over_the_limit_is_refunded_instead_of_credited(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    seed_reservation(database, wallet, amount=30.0, payment_reference="pi_late",
                     expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                     status=TopUpReservationStatus.EXPIRED)
    with daily_limit_settings():
        with stripe_refund("re_1") as refund_call:
            credit(wallet_service, amount=30.0, payment_reference="pi_late")

    assert wallet["amount"] == 0.0
    assert len(successful_top_ups(database, wallet)) == 1
    refund_call.assert_called_once()
    assert refund_call.call_args.kwargs["payment_intent_id"] == "pi_late"
    assert refund_call.call_args.kwargs["idempotency_key"] == "wallet-top-up-refund-pi_late"
    refund = refunds_of(database)[0]
    assert refund["status"] == TopUpRefundStatus.SUCCEEDED
    assert refund["reason"] == TopUpRefundReason.AMOUNT_LIMIT_EXCEEDED
    assert refund["provider_refund_reference"] == "re_1"
    assert refund["payment_reference"] == "pi_late"
    assert refund["attempt_count"] == 1


def test_paid_top_up_without_any_reservation_is_refunded(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=10.0)
    seed_transaction(database, wallet, amount=10.0)
    with daily_limit_settings():
        with stripe_refund("re_2") as refund_call:
            credit(wallet_service, amount=20.0, payment_reference="pi_orphan")

    assert wallet["amount"] == 0.0
    refund_call.assert_called_once()
    refund = refunds_of(database)[0]
    assert refund["reason"] == TopUpRefundReason.COUNT_LIMIT_REACHED
    assert refund["status"] == TopUpRefundStatus.SUCCEEDED


def test_replayed_webhook_does_not_create_a_second_refund(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with stripe_refund("re_3") as refund_call:
            credit(wallet_service, amount=30.0, payment_reference="pi_replayed")
            credit(wallet_service, amount=30.0, payment_reference="pi_replayed")
            credit(wallet_service, amount=30.0, payment_reference="pi_replayed")

    assert len(refunds_of(database)) == 1
    # the refund succeeded on the first callback, the replays do not call the provider again
    refund_call.assert_called_once()
    assert wallet["amount"] == 0.0
    assert len(successful_top_ups(database, wallet)) == 1


def test_credited_payment_is_never_refunded_by_a_replayed_webhook(database, wallet_service):
    wallet = seed_wallet(database)
    seed_reservation(database, wallet, amount=40.0, payment_reference="pi_credited")
    with daily_limit_settings():
        with stripe_refund() as refund_call:
            credit(wallet_service, amount=40.0, payment_reference="pi_credited")
            # the capacity is used up by then, the replay must not turn the credit into a refund
            credit(wallet_service, amount=40.0, payment_reference="pi_credited")

    assert wallet["amount"] == 40.0
    assert len(successful_top_ups(database, wallet)) == 1
    refund_call.assert_not_called()
    assert refunds_of(database) == []


def test_refunded_payment_is_never_credited_by_a_replayed_webhook(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with stripe_refund("re_4"):
            credit(wallet_service, amount=30.0, payment_reference="pi_refunded")
        # capacity frees up afterwards, the replayed callback must still not credit it
        database.rows("user_wallet_transaction").clear()
        with stripe_refund("re_4"):
            credit(wallet_service, amount=30.0, payment_reference="pi_refunded")

    assert wallet["amount"] == 0.0
    assert successful_top_ups(database, wallet) == []
    assert len(refunds_of(database)) == 1


def test_refund_failure_leaves_a_retryable_record(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with stripe_refund(error=RuntimeError("stripe is down")):
            credit(wallet_service, amount=30.0, payment_reference="pi_retry")

    refund = refunds_of(database)[0]
    assert refund["status"] == TopUpRefundStatus.FAILED
    assert refund["attempt_count"] == 1
    assert "stripe is down" in refund["last_error"]
    assert refund["provider_refund_reference"] is None
    assert wallet["amount"] == 0.0


def test_refund_retry_succeeds_and_persists_the_provider_reference(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with stripe_refund(error=RuntimeError("stripe is down")):
            credit(wallet_service, amount=30.0, payment_reference="pi_retry")
        with stripe_refund("re_retry") as refund_call:
            retried = wallet_service.retry_pending_top_up_refunds()

    assert retried == 1
    refund_call.assert_called_once()
    refund = refunds_of(database)[0]
    assert refund["status"] == TopUpRefundStatus.SUCCEEDED
    assert refund["provider_refund_reference"] == "re_retry"
    assert refund["attempt_count"] == 2
    assert refund["last_error"] is None
    assert wallet["amount"] == 0.0


def test_refund_retry_does_nothing_once_every_refund_succeeded(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    with daily_limit_settings():
        with stripe_refund("re_done"):
            credit(wallet_service, amount=30.0, payment_reference="pi_done")
        with stripe_refund("re_again") as refund_call:
            retried = wallet_service.retry_pending_top_up_refunds()
    assert retried == 0
    refund_call.assert_not_called()


def test_callback_keeps_the_legacy_flow_when_the_feature_flag_is_disabled(database, wallet_service):
    wallet = seed_wallet(database, amount=5.0)
    seed_transaction(database, wallet, amount=500.0)
    seed_transaction(database, wallet, amount=500.0)
    with daily_limit_settings(DAILY_TOP_LIMIT='false'):
        credit(wallet_service, amount=300.0)
    assert wallet["amount"] == 305.0
    assert len(successful_top_ups(database, wallet)) == 3
    assert database.rpc_calls == []
    assert database.rows("user_wallet_top_up_reservation") == []
    assert refunds_of(database) == []


# --- callback service wiring ------------------------------------------------------

def build_callback_service(wallet_service, order):
    service = object.__new__(CallbackService)
    service._CallbackService__user_wallet_service = wallet_service
    service._CallbackService__user_order_repo = MagicMock()
    service._CallbackService__user_order_repo.get_by_id.return_value = order
    service._CallbackService__task_executor = MagicMock()
    service._CallbackService__task_executor.add_task.side_effect = lambda task: task()
    return service


def handle_webhook(service, wallet, order, event_type: str):
    metadata = {"user_wallet_id": wallet["id"], "user_id": USER_ID, "order_id": order.id}
    with patch('app.services.callback_service.fcm_service') as fcm:
        service._CallbackService__handle_wallet_top_up(metadata, event_type)
    return fcm


def test_payment_webhook_credits_the_wallet_and_ignores_replays(database, wallet_service):
    wallet = seed_wallet(database)
    order = MagicMock(id="order-1", amount=40.0, currency="USD", payment_intent_code="pi_webhook")
    reservation = seed_reservation(database, wallet, amount=40.0, order_id=order.id,
                                   payment_reference=order.payment_intent_code)
    service = build_callback_service(wallet_service, order)

    with daily_limit_settings():
        handle_webhook(service, wallet, order, "payment_intent.succeeded")
        handle_webhook(service, wallet, order, "payment_intent.succeeded")

    assert wallet["amount"] == 40.0
    assert len(successful_top_ups(database, wallet)) == 1
    assert reservation["status"] == "completed"


def test_payment_webhook_refunds_a_payment_that_cannot_be_credited(database, wallet_service):
    wallet = seed_wallet(database)
    seed_transaction(database, wallet, amount=80.0)
    order = MagicMock(id="order-5", amount=30.0, currency="USD", payment_intent_code="pi_webhook_refund")
    seed_reservation(database, wallet, amount=30.0, order_id=order.id,
                     payment_reference=order.payment_intent_code,
                     status=TopUpReservationStatus.EXPIRED,
                     expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    service = build_callback_service(wallet_service, order)

    with daily_limit_settings():
        with stripe_refund("re_webhook") as refund_call:
            with patch('app.services.user_wallet_service.fcm_service') as wallet_fcm:
                handle_webhook(service, wallet, order, "payment_intent.succeeded")
                # the replayed webhook must not refund a second time either
                handle_webhook(service, wallet, order, "payment_intent.succeeded")

    assert wallet["amount"] == 0.0
    assert len(successful_top_ups(database, wallet)) == 1
    refund_call.assert_called_once()
    assert len(refunds_of(database)) == 1
    assert refunds_of(database)[0]["provider_refund_reference"] == "re_webhook"
    # the user is told the top-up did not go through
    wallet_fcm.send_notification_to_user_from_template.assert_called()


def test_failed_payment_webhook_releases_the_reservation(database, wallet_service):
    wallet = seed_wallet(database)
    order = MagicMock(id="order-2", amount=90.0, currency="USD", payment_intent_code="pi_failed")
    reservation = seed_reservation(database, wallet, amount=90.0, order_id=order.id,
                                   payment_reference=order.payment_intent_code)
    service = build_callback_service(wallet_service, order)

    with daily_limit_settings():
        handle_webhook(service, wallet, order, "payment_intent.failed")
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)
        # the released capacity is immediately available again
        response, _, _ = top_up(wallet_service, amount=90.0)

    assert reservation["status"] == TopUpReservationStatus.CANCELLED
    assert (usage.successful_topup_count, usage.pending_reservation_count) == (0, 0)
    assert wallet["amount"] == 0.0
    assert response.status == "success"


def test_cancelled_payment_webhook_releases_the_reservation(database, wallet_service):
    wallet = seed_wallet(database)
    order = MagicMock(id="order-3", amount=100.0, currency="USD", payment_intent_code="pi_cancelled")
    reservation = seed_reservation(database, wallet, amount=100.0, order_id=order.id,
                                   payment_reference=order.payment_intent_code)
    service = build_callback_service(wallet_service, order)

    with daily_limit_settings():
        handle_webhook(service, wallet, order, "payment_intent.canceled")
        # replaying the cancellation is a no-op and never reverts a completed reservation
        handle_webhook(service, wallet, order, "payment_intent.canceled")
        usage = wallet_service.get_daily_top_up_usage(user_id=USER_ID)

    assert reservation["status"] == TopUpReservationStatus.CANCELLED
    assert usage.reserved_count == 0
    assert len(successful_top_ups(database, wallet)) == 0


def test_failed_webhook_after_a_successful_one_never_reverts_the_credit(database, wallet_service):
    wallet = seed_wallet(database)
    order = MagicMock(id="order-4", amount=40.0, currency="USD", payment_intent_code="pi_mixed")
    reservation = seed_reservation(database, wallet, amount=40.0, order_id=order.id,
                                   payment_reference=order.payment_intent_code)
    service = build_callback_service(wallet_service, order)

    with daily_limit_settings():
        handle_webhook(service, wallet, order, "payment_intent.succeeded")
        handle_webhook(service, wallet, order, "payment_intent.failed")

    assert reservation["status"] == TopUpReservationStatus.COMPLETED
    assert wallet["amount"] == 40.0
    assert len(successful_top_ups(database, wallet)) == 1


def test_full_flow_never_leaves_a_paid_top_up_uncredited(database, wallet_service):
    """Every successful payment ends up credited, whatever happened to its reservation."""
    wallet = seed_wallet(database)
    service = build_callback_service(wallet_service, None)

    with daily_limit_settings():
        # one request wins the capacity, a concurrent one is rejected before being charged
        outcomes = concurrent_top_ups(database, wallet_service, [60.0, 60.0])
        reserved = pending_reservations(database, wallet)[0]
        # the winner is charged and its reservation expires before the callback arrives
        reserved["expires_at"] = utc_iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        order = MagicMock(id=reserved["order_id"], amount=reserved["amount"], currency="USD",
                          payment_intent_code=reserved["payment_reference"])
        service._CallbackService__user_order_repo.get_by_id.return_value = order
        handle_webhook(service, wallet, order, "payment_intent.succeeded")

    assert sorted(outcomes) == sorted(["reserved", str(ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED)])
    assert wallet["amount"] == 60.0
    assert len(successful_top_ups(database, wallet)) == 1
    assert reserved["status"] == TopUpReservationStatus.COMPLETED
