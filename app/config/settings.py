import os
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_PATH = os.path.abspath(os.curdir)


def _env_file_path() -> str:
    environment = os.getenv("ENVIRONMENT")
    return f"{ROOT_PATH}/.env" if not environment else f"{ROOT_PATH}/.env.{environment}"


class Settings(BaseSettings):
    """Typed application settings loaded from the environment.

    Field names map to their upper-cased environment variable (``daily_top_limit`` ->
    ``DAILY_TOP_LIMIT``); every value has a safe default so the application keeps
    working when the variables are not provided.
    """

    # Daily wallet top-up limits (disabled by default)
    daily_top_limit: bool = False
    daily_top_up_max_count: int = 2
    daily_top_up_max_amount_usd: Decimal = Decimal("100.00")
    daily_top_up_limit_timezone: str = "UTC"
    # how long a daily limit reservation holds capacity while the payment is pending
    daily_top_up_reservation_ttl_minutes: int = 30

    model_config = SettingsConfigDict(env_file=_env_file_path(), env_file_encoding="utf-8",
                                      case_sensitive=False, extra="ignore")

    @field_validator("daily_top_up_max_count")
    @classmethod
    def validate_max_count(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("DAILY_TOP_UP_MAX_COUNT must be greater than zero")
        return value

    @field_validator("daily_top_up_reservation_ttl_minutes")
    @classmethod
    def validate_reservation_ttl(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("DAILY_TOP_UP_RESERVATION_TTL_MINUTES must be greater than zero")
        return value

    @field_validator("daily_top_up_max_amount_usd")
    @classmethod
    def validate_max_amount(cls, value: Decimal) -> Decimal:
        if value <= Decimal("0"):
            raise ValueError("DAILY_TOP_UP_MAX_AMOUNT_USD must be greater than zero")
        return value

    @field_validator("daily_top_up_limit_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"DAILY_TOP_UP_LIMIT_TIMEZONE '{value}' is not a valid timezone") from e
        return value

    @property
    def daily_top_up_limit_zone_info(self) -> ZoneInfo:
        return ZoneInfo(self.daily_top_up_limit_timezone)

    @property
    def daily_top_up_max_amount_usd_display(self) -> str:
        """Configured daily maximum rendered without a useless fractional part (``100`` not ``100.00``)."""
        amount = self.daily_top_up_max_amount_usd
        return f"{amount.quantize(Decimal('1')) if amount == amount.to_integral_value() else amount:f}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def validate_settings() -> Settings:
    """Validate the typed settings at application startup and fail fast on bad values."""
    settings = get_settings()
    if settings.daily_top_limit:
        logger.info(f"daily top-up limits enabled: max_count={settings.daily_top_up_max_count}, "
                    f"max_amount_usd={settings.daily_top_up_max_amount_usd_display}, "
                    f"timezone={settings.daily_top_up_limit_timezone}, "
                    f"reservation_ttl_minutes={settings.daily_top_up_reservation_ttl_minutes}")
    return settings
