"""Request and response shapes of /api/v1/admin/vouchers (MS-5089, TEOS-133)."""
from datetime import date, datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VoucherStatus = Literal["all", "active", "inactive", "used", "expired"]

#: TEOS-133: letters, digits and "-", 4 to 32 characters. Letters are accepted in any case and stored upper
#: case. Codes stored before (other shapes) are not re-validated: list, status and delete keep working.
VOUCHER_CODE_PATTERN = r"^[A-Za-z0-9-]{4,32}$"
#: Fallback of the VOUCHER_MAX_AMOUNT setting (seeded with this value in supabase_ddl.sql): the most a
#: voucher can be worth when the setting row is missing or not a positive number.
MAX_VOUCHER_AMOUNT = 1_000_000
#: Server-generated codes: 12 characters from this alphabet (no 0/O, 1/I/L), drawn with ``secrets``.
GENERATED_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
GENERATED_CODE_LENGTH = 12


class AdminVoucherCreate(BaseModel):
    """Body of ``POST /vouchers``. ``code`` is optional: omitted (or null) the server generates one;
    given, it is stored upper case. ``amount`` > 0 and at most the VOUCHER_MAX_AMOUNT setting (checked by the
    service). ``expired_at`` is a calendar day (``YYYY-MM-DD``), today or later."""
    code: Optional[str] = Field(None, pattern=VOUCHER_CODE_PATTERN)
    amount: float = Field(..., gt=0)
    expired_at: Optional[date] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("code")
    @classmethod
    def _upper(cls, value: Optional[str]) -> Optional[str]:
        return value.upper() if value is not None else None

    @field_validator("expired_at", mode="before")
    @classmethod
    def _day_only(cls, value):
        # The portal date input sends "YYYY-MM-DD"; a full timestamp is reduced to its day.
        if isinstance(value, str) and "T" in value:
            return value.split("T", 1)[0]
        if isinstance(value, datetime):
            return value.date()
        return value

    @field_validator("expired_at")
    @classmethod
    def _not_in_the_past(cls, value: Optional[date]) -> Optional[date]:
        if value is not None and value < datetime.now(timezone.utc).date():
            raise ValueError("expired_at must be today or later")
        return value


class AdminVoucherUser(BaseModel):
    id: str
    email: Optional[str] = None


class AdminVoucher(BaseModel):
    """One row of the vouchers list. ``used_by`` (TEOS-133) and ``used_by_user`` (MS-5089, kept for
    compatibility) are the same ``{id, email}`` of the user who redeemed it. ``is_expired``: unused and
    ``expired_at`` in the past. ``currency``: the default currency of the deployment."""
    id: int
    code: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    is_used: bool = False
    is_active: bool = True
    is_expired: bool = False
    expired_at: Optional[str] = None
    created_at: Optional[str] = None
    used_by: Optional[AdminVoucherUser] = None
    used_by_user: Optional[AdminVoucherUser] = None
