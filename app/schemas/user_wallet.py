from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class UserWalletResponse(BaseModel):
    balance: float
    currency: str


class UserWalletRequestDto(BaseModel):
    user_id: str
    amount: float
    currency: str


class TopUpWalletRequest(BaseModel):
    amount: float


class DailyTopUpUsage(BaseModel):
    """Daily top-up capacity already used by a user inside one daily window.

    Completed top-ups and pending reservations are reported separately: only the
    `successful_*` values are actual top-ups, while the `pending_reservation_*` values
    are unfinished payments that still hold capacity until they complete or expire.
    Amounts are expressed in `currency` (the wallet currency) and normalized to USD by
    the service before being compared with the configured limit.
    """
    successful_topup_count: int = 0
    successful_topup_amount: Decimal = Decimal("0")
    pending_reservation_count: int = 0
    pending_reservation_amount: Decimal = Decimal("0")
    currency: str

    @property
    def reserved_count(self) -> int:
        return self.successful_topup_count + self.pending_reservation_count

    @property
    def reserved_amount(self) -> Decimal:
        return self.successful_topup_amount + self.pending_reservation_amount


class TopUpReservationResult(BaseModel):
    """Outcome of the atomic (locked) daily limit reservation taken before paying."""
    status: str
    reservation_id: Optional[str] = None
    successful_topup_count: int = 0
    successful_topup_amount: Decimal = Decimal("0")
    pending_reservation_count: int = 0
    pending_reservation_amount: Decimal = Decimal("0")

    @property
    def reserved_count(self) -> int:
        return self.successful_topup_count + self.pending_reservation_count

    @property
    def reserved_amount(self) -> Decimal:
        return self.successful_topup_amount + self.pending_reservation_amount


class TopUpCreditResult(BaseModel):
    """Outcome of the atomic (locked) settlement of a paid top-up.

    Either the wallet was credited (`credited` / `already_processed`), or the top-up cannot
    be credited without breaking the daily limits and a refund record was persisted in the
    same transaction (`refund_required`).
    """
    status: str
    transaction_id: Optional[str] = None
    reservation_id: Optional[str] = None
    reservation_status: Optional[str] = None
    balance: Optional[Decimal] = None
    refund_id: Optional[str] = None
    refund_status: Optional[str] = None
    refund_reason: Optional[str] = None
    provider_refund_reference: Optional[str] = None
    refund_amount: Optional[Decimal] = None
    refund_currency: Optional[str] = None
    attempt_count: int = 0
