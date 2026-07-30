import asyncio
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.config.constants import ErrorMessages
from app.config.settings import Settings, get_settings, validate_settings
from app.exceptions import CustomException
from app.i18n import translate
from app.schemas.user_wallet import DailyTopUpUsage
from app.services.user_wallet_service import UserWalletService, daily_top_up_window

DAILY_LIMIT_ENV = {
    "DAILY_TOP_LIMIT": "true",
    "DAILY_TOP_UP_MAX_COUNT": "2",
    "DAILY_TOP_UP_MAX_AMOUNT_USD": "100",
    "DAILY_TOP_UP_LIMIT_TIMEZONE": "UTC",
}


@contextmanager
def daily_limit_settings(**overrides):
    """Run a block with the daily top-up settings loaded from the given environment."""
    env = {**DAILY_LIMIT_ENV, **{key: str(value) for key, value in overrides.items()}}
    get_settings.cache_clear()
    with patch.dict(os.environ, env):
        try:
            yield get_settings()
        finally:
            get_settings.cache_clear()


@pytest.fixture
def wallet_service():
    with patch('app.services.user_wallet_service.UserWalletRepo') as MockWalletRepo, \
         patch('app.services.user_wallet_service.UserOrderRepo') as MockOrderRepo, \
         patch('app.services.user_wallet_service.UserWalletTransactionRepo') as MockTransRepo, \
         patch('app.services.user_wallet_service.UserRepo') as MockUserRepo, \
         patch('app.services.user_wallet_service.UserWalletTopUpReservationRepo') as MockReservationRepo, \
         patch('app.services.user_wallet_service.CurrencyService') as MockCurrencyService:
        service = UserWalletService()
        service._UserWalletService__user_wallet_repo = MockWalletRepo()
        service._UserWalletService__user_order_repo = MockOrderRepo()
        service._UserWalletService__user_wallet_transaction_repo = MockTransRepo()
        service._UserWalletService__user_repo = MockUserRepo()
        service._UserWalletService__top_up_reservation_repo = MockReservationRepo()
        service._UserWalletService__currency_service = MockCurrencyService()
        service._UserWalletService__user_wallet_repo.get_first_by.return_value = MagicMock(id='wid', currency='USD',
                                                                                           amount=0.0)
        service._UserWalletService__top_up_reservation_repo.get_active_daily_usage.return_value = (0, Decimal("0"))
        return service


def set_usage(service, count: int, amount: str, currency: str = 'USD', pending_count: int = 0,
              pending_amount: str = '0'):
    usage = DailyTopUpUsage(successful_topup_count=count, successful_topup_amount=Decimal(amount), currency=currency)
    service._UserWalletService__user_wallet_transaction_repo.get_daily_top_up_usage.return_value = usage
    service._UserWalletService__top_up_reservation_repo.get_active_daily_usage.return_value = (
        pending_count, Decimal(pending_amount))
    return usage


def validate(service, amount: str, user_id: str = 'user-1'):
    return asyncio.run(service.validate_daily_top_up_limits(user_id=user_id, requested_amount_usd=Decimal(amount)))


# --- feature flag -----------------------------------------------------------------

def test_validation_is_skipped_when_feature_flag_is_disabled(wallet_service):
    set_usage(wallet_service, count=9, amount='900')
    with daily_limit_settings(DAILY_TOP_LIMIT='false'):
        assert validate(wallet_service, '500') is None
    wallet_service._UserWalletService__user_wallet_transaction_repo.get_daily_top_up_usage.assert_not_called()


def test_feature_flag_defaults_to_disabled():
    get_settings.cache_clear()
    try:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DAILY_TOP_LIMIT", None)
            assert Settings(_env_file=None).daily_top_limit is False
    finally:
        get_settings.cache_clear()


# --- limits -----------------------------------------------------------------------

def test_first_top_up_of_the_day_is_allowed(wallet_service):
    set_usage(wallet_service, count=0, amount='0')
    with daily_limit_settings():
        assert validate(wallet_service, '40') is None


def test_second_top_up_within_both_limits_is_allowed(wallet_service):
    set_usage(wallet_service, count=1, amount='40')
    with daily_limit_settings():
        assert validate(wallet_service, '50') is None


def test_count_limit_is_rejected(wallet_service):
    set_usage(wallet_service, count=2, amount='30')
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '10')
    assert error.value.name == ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED
    assert translate(error.value.name) == "You have reached the maximum number of top-ups allowed per day."


def test_amount_limit_is_rejected(wallet_service):
    set_usage(wallet_service, count=1, amount='80')
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '30')
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED
    assert translate(error.value.name, params=error.value.params) == (
        "This transaction cannot be completed because the maximum daily top-up amount of USD 100 "
        "would be exceeded.")


def test_exact_limit_is_allowed(wallet_service):
    set_usage(wallet_service, count=1, amount='70')
    with daily_limit_settings():
        assert validate(wallet_service, '30') is None


def test_one_cent_over_the_limit_is_rejected(wallet_service):
    set_usage(wallet_service, count=1, amount='70')
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '30.01')
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED


def test_count_limit_takes_precedence_over_amount_limit(wallet_service):
    set_usage(wallet_service, count=2, amount='10')
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '1')
    assert error.value.name == ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED


def test_configured_maximums_are_enforced(wallet_service):
    set_usage(wallet_service, count=2, amount='40')
    with daily_limit_settings(DAILY_TOP_UP_MAX_COUNT=5, DAILY_TOP_UP_MAX_AMOUNT_USD=50):
        assert validate(wallet_service, '10') is None
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '10.5')
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED
    assert error.value.params == {"amount": "50"}


def test_configured_amount_is_reported_without_useless_decimals(wallet_service):
    set_usage(wallet_service, count=0, amount='0')
    with daily_limit_settings(DAILY_TOP_UP_MAX_AMOUNT_USD='250.50'):
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '250.51')
    assert error.value.params == {"amount": "250.50"}


def test_pending_reservations_reduce_the_available_amount(wallet_service):
    set_usage(wallet_service, count=1, amount='40', pending_count=1, pending_amount='50')
    with daily_limit_settings(DAILY_TOP_UP_MAX_COUNT=5):
        assert validate(wallet_service, '10') is None
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '10.01')
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED


def test_pending_reservations_reduce_the_available_count(wallet_service):
    set_usage(wallet_service, count=1, amount='10', pending_count=1, pending_amount='10')
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '5')
    assert error.value.name == ErrorMessages.TOP_UP_COUNT_LIMIT_REACHED


def test_pending_reservations_are_reported_apart_from_successful_top_ups(wallet_service):
    set_usage(wallet_service, count=1, amount='40', pending_count=2, pending_amount='30')
    with daily_limit_settings():
        usage = wallet_service.get_daily_top_up_usage(user_id='user-1')
    assert (usage.successful_topup_count, usage.successful_topup_amount) == (1, Decimal("40"))
    assert (usage.pending_reservation_count, usage.pending_reservation_amount) == (2, Decimal("30"))
    assert (usage.reserved_count, usage.reserved_amount) == (3, Decimal("70"))


def test_active_reservations_are_read_for_the_current_window(wallet_service):
    set_usage(wallet_service, count=0, amount='0')
    with daily_limit_settings():
        validate(wallet_service, '10')
        expected_start, expected_end = daily_top_up_window()
    call = wallet_service._UserWalletService__top_up_reservation_repo.get_active_daily_usage.call_args.kwargs
    assert call["window_start"] == expected_start
    assert call["window_end"] == expected_end
    assert call["window_start"].tzinfo is not None


def test_usage_is_read_for_the_requested_user_only(wallet_service):
    set_usage(wallet_service, count=0, amount='0')
    with daily_limit_settings():
        validate(wallet_service, '10', user_id='user-42')
    wallet_service._UserWalletService__user_wallet_repo.get_first_by.assert_called_with(where={"user_id": "user-42"})


def test_user_without_wallet_has_no_daily_usage(wallet_service):
    wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = None
    with daily_limit_settings():
        assert validate(wallet_service, '100') is None
    wallet_service._UserWalletService__user_wallet_transaction_repo.get_daily_top_up_usage.assert_not_called()


def test_usage_amount_is_normalized_to_usd(wallet_service):
    set_usage(wallet_service, count=1, amount='90', currency='EUR')
    wallet_service._UserWalletService__currency_service.convert.return_value = 99.0
    with daily_limit_settings():
        with pytest.raises(CustomException) as error:
            validate(wallet_service, '1.5')
    assert error.value.name == ErrorMessages.DAILY_TOP_UP_AMOUNT_LIMIT_EXCEEDED
    wallet_service._UserWalletService__currency_service.convert.assert_called_with(from_currency='EUR',
                                                                                   to_currency='USD', amount=90.0)


# --- daily window -----------------------------------------------------------------

def test_daily_window_is_the_utc_day_by_default():
    with daily_limit_settings():
        start, end = daily_top_up_window(datetime(2026, 7, 30, 13, 45, tzinfo=timezone.utc))
    assert start == datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)


def test_daily_window_follows_the_configured_timezone():
    with daily_limit_settings(DAILY_TOP_UP_LIMIT_TIMEZONE='Asia/Beirut'):
        # 2026-07-30 22:30 UTC is already 2026-07-31 in Beirut (UTC+3 in summer)
        start, end = daily_top_up_window(datetime(2026, 7, 30, 22, 30, tzinfo=timezone.utc))
    assert start == datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)


def test_daily_window_moves_to_the_next_day():
    with daily_limit_settings():
        today = daily_top_up_window(datetime(2026, 7, 30, 23, 59, tzinfo=timezone.utc))
        tomorrow = daily_top_up_window(datetime(2026, 7, 31, 0, 1, tzinfo=timezone.utc))
    assert today[1] == tomorrow[0]
    assert today[0] != tomorrow[0]


def test_usage_query_uses_the_current_window_and_successful_top_ups_only(wallet_service):
    set_usage(wallet_service, count=0, amount='0')
    with daily_limit_settings():
        validate(wallet_service, '10')
        expected_start, expected_end = daily_top_up_window()
    call = wallet_service._UserWalletService__user_wallet_transaction_repo.get_daily_top_up_usage.call_args.kwargs
    assert call["source"] == "TOP-UP-WALLET"
    assert call["status"] == "success"
    assert call["window_start"] == expected_start
    assert call["window_end"] == expected_end
    assert call["window_start"].tzinfo is not None


# --- settings validation ----------------------------------------------------------

def build_settings(**overrides) -> Settings:
    """Load the settings the way the application does at startup."""
    get_settings.cache_clear()
    try:
        with patch.dict(os.environ, {**DAILY_LIMIT_ENV, **{k: str(v) for k, v in overrides.items()}}):
            return validate_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("overrides, expected_message", [
    ({"DAILY_TOP_UP_MAX_COUNT": "0"}, "DAILY_TOP_UP_MAX_COUNT must be greater than zero"),
    ({"DAILY_TOP_UP_MAX_COUNT": "-1"}, "DAILY_TOP_UP_MAX_COUNT must be greater than zero"),
    ({"DAILY_TOP_UP_MAX_AMOUNT_USD": "0"}, "DAILY_TOP_UP_MAX_AMOUNT_USD must be greater than zero"),
    ({"DAILY_TOP_UP_MAX_AMOUNT_USD": "0.00"}, "DAILY_TOP_UP_MAX_AMOUNT_USD must be greater than zero"),
    ({"DAILY_TOP_UP_MAX_AMOUNT_USD": "-10"}, "DAILY_TOP_UP_MAX_AMOUNT_USD must be greater than zero"),
    ({"DAILY_TOP_UP_RESERVATION_TTL_MINUTES": "0"}, "DAILY_TOP_UP_RESERVATION_TTL_MINUTES must be greater than zero"),
    ({"DAILY_TOP_UP_LIMIT_TIMEZONE": "Not/AZone"}, "is not a valid timezone"),
])
def test_invalid_configuration_is_rejected_at_startup(overrides, expected_message):
    with pytest.raises(ValidationError) as error:
        build_settings(**overrides)
    assert expected_message in str(error.value)


def test_zero_maximum_count_is_rejected_at_startup():
    with pytest.raises(ValidationError):
        build_settings(DAILY_TOP_UP_MAX_COUNT=0)


def test_negative_maximum_count_is_rejected_at_startup():
    with pytest.raises(ValidationError):
        build_settings(DAILY_TOP_UP_MAX_COUNT=-3)


def test_zero_maximum_amount_is_rejected_at_startup():
    with pytest.raises(ValidationError):
        build_settings(DAILY_TOP_UP_MAX_AMOUNT_USD=0)


def test_negative_maximum_amount_is_rejected_at_startup():
    with pytest.raises(ValidationError):
        build_settings(DAILY_TOP_UP_MAX_AMOUNT_USD=-0.01)


def test_positive_configuration_is_accepted_at_startup():
    settings = build_settings(DAILY_TOP_UP_MAX_COUNT=3, DAILY_TOP_UP_MAX_AMOUNT_USD="250.50",
                              DAILY_TOP_UP_RESERVATION_TTL_MINUTES=15, DAILY_TOP_UP_LIMIT_TIMEZONE="Asia/Beirut")
    assert settings.daily_top_limit is True
    assert settings.daily_top_up_max_count == 3
    assert settings.daily_top_up_max_amount_usd == Decimal("250.50")
    assert settings.daily_top_up_reservation_ttl_minutes == 15
    assert settings.daily_top_up_limit_timezone == "Asia/Beirut"
