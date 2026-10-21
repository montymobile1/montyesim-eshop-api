"""Request and response shapes of /api/v1/admin/vouchers (MS-5089)."""
from datetime import date, datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VoucherStatus = Literal["all", "active", "inactive"]

#: Same rule as the portal form: letters, digits, "_" and "-", 2 to 50 characters.
VOUCHER_CODE_PATTERN = r"^[A-Za-z0-9_-]{2,50}$"
MAX_VOUCHER_AMOUNT = 1_000_000


class AdminVoucherCreate(BaseModel):
    """Body of ``POST /vouchers``. ``expired_at`` is a calendar day (``YYYY-MM-DD``), today or later."""
    code: str = Field(..., pattern=VOUCHER_CODE_PATTERN)
    amount: float = Field(..., gt=0, le=MAX_VOUCHER_AMOUNT)
    expired_at: Optional[date] = None

    model_config = ConfigDict(extra="forbid")

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
    """One row of the vouchers list."""
    id: int
    code: Optional[str] = None
    amount: Optional[float] = None
    is_used: bool = False
    is_active: bool = True
    expired_at: Optional[str] = None
    created_at: Optional[str] = None
    used_by_user: Optional[AdminVoucherUser] = None
