"""Request and response shapes of /api/v1/admin/promotions (MS-5050 list, MS-5052 add/edit, MS-5054 usage)."""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.admin.common import AdminPage, StrictNumber

PromotionStatus = Literal["all", "active", "inactive", "expired", "scheduled"]

#: Same rule as the voucher code: letters, digits, "_" and "-", 2 to 50 characters.
PROMOTION_CODE_PATTERN = r"^[A-Za-z0-9_-]{2,50}$"
MAX_BUNDLE_CODES = 5  # 5 codes of up to 36 chars + 4 commas fit the varchar(200) column
MAX_BUNDLE_CODE_LENGTH = 36
MAX_AMOUNT = 1_000_000
MAX_USAGE_DAYS = 366


def _clean_bundle_codes(codes: Optional[List[str]]) -> List[str]:
    """Trimmed, de-duplicated codes; 422 for blanks, commas (the column is a CSV) or overlong codes."""
    cleaned: List[str] = []
    for code in codes or []:
        text = str(code).strip()
        if not text:
            raise ValueError("bundle_codes must not contain blank codes")
        if "," in text:
            raise ValueError("a bundle code cannot contain a comma")
        if len(text) > MAX_BUNDLE_CODE_LENGTH:
            raise ValueError(f"a bundle code is at most {MAX_BUNDLE_CODE_LENGTH} characters")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_BUNDLE_CODES:
        raise ValueError(f"at most {MAX_BUNDLE_CODES} bundle codes")
    return cleaned


class AdminPromotionCreate(BaseModel):
    """Body of ``POST /promotions``.

    The server sets ``type`` ("Promo"), ``is_active`` (true) and ``times_used`` (0); ``callback_url`` and
    ``callback_headers`` can never be set through the API (unknown fields -> 422). ``amount`` is required
    when the rule's action is amount-based and must be at most 100 for a percentage action (checked by the
    service, which knows the rule). ``bundle_codes`` empty = the promotion applies to every bundle.
    """
    name: str = Field(..., min_length=2, max_length=100)
    code: str = Field(..., pattern=PROMOTION_CODE_PATTERN)
    rule_id: UUID
    amount: Optional[StrictNumber] = Field(None, gt=0, le=MAX_AMOUNT)  # TEOS-228: "5" is a 422
    valid_from: datetime
    valid_to: datetime
    bundle_codes: List[str] = Field(default_factory=list, max_length=MAX_BUNDLE_CODES)

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if len(value.strip()) < 2:
            raise ValueError("name must have at least 2 characters")
        return value.strip()

    @field_validator("bundle_codes")
    @classmethod
    def _bundle_codes(cls, value: Optional[List[str]]) -> List[str]:
        return _clean_bundle_codes(value)

    @model_validator(mode="after")
    def _dates_in_order(self):
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class AdminPromotionUpdate(BaseModel):
    """Body of ``PUT /promotions/{id}``.

    ``name``, ``bundle_codes`` and ``valid_to`` can always change. ``code``, ``rule_id``, ``amount`` and
    ``valid_from`` are optional here and immutable once the promotion has been used: sending a different
    value for one of them when ``times_used > 0`` returns 409 ``promotion_used``.

    ``bundle_codes`` (TEOS-226): missing or null keeps the promotion's bundles; only an explicit ``[]`` makes
    it apply to every bundle.
    """
    name: str = Field(..., min_length=2, max_length=100)
    bundle_codes: Optional[List[str]] = Field(None, max_length=MAX_BUNDLE_CODES)
    valid_to: Optional[datetime] = None
    code: Optional[str] = Field(None, pattern=PROMOTION_CODE_PATTERN)
    rule_id: Optional[UUID] = None
    amount: Optional[StrictNumber] = Field(None, gt=0, le=MAX_AMOUNT)  # TEOS-228: "5" is a 422
    valid_from: Optional[datetime] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if len(value.strip()) < 2:
            raise ValueError("name must have at least 2 characters")
        return value.strip()

    @field_validator("bundle_codes")
    @classmethod
    def _bundle_codes(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return None if value is None else _clean_bundle_codes(value)

    @model_validator(mode="after")
    def _dates_in_order(self):
        if self.valid_from is not None and self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class AdminPromotionRuleRef(BaseModel):
    id: int
    name: Optional[str] = None


class AdminPromotionRuleEmbed(BaseModel):
    """The rule of a promotion, with its action and event."""
    id: str
    name: Optional[str] = None
    max_usage: Optional[int] = None
    beneficiary: Optional[int] = None
    action: Optional[AdminPromotionRuleRef] = None
    event: Optional[AdminPromotionRuleRef] = None


class AdminPromotion(BaseModel):
    """One promotion as listed and returned by every route. ``bundle_codes`` is the stored CSV split
    server-side; ``status`` is the list tab the row belongs to (active, inactive, expired or scheduled).
    ``callback_url`` / ``callback_headers`` are never returned."""
    id: str
    name: Optional[str] = None
    code: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[float] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    is_active: bool = True
    times_used: int = 0
    bundle_codes: List[str] = Field(default_factory=list)
    rule_id: Optional[str] = None
    rule: Optional[AdminPromotionRuleEmbed] = None
    status: Optional[str] = None
    created_at: Optional[str] = None


class AdminPromotionPage(AdminPage[AdminPromotion]):
    """``facets`` = ``{all, active, inactive, expired, scheduled}`` over the searched set (search, rules,
    bundles), whatever the ``status`` filter."""


# ---------------------------------------------------------------- usage (MS-5054)

class AdminPromotionUsagePromotion(BaseModel):
    id: str
    code: Optional[str] = None
    name: Optional[str] = None


class AdminPromotionUsageUser(BaseModel):
    id: str
    email: Optional[str] = None
    msisdn: Optional[str] = None


class AdminPromotionUsageRow(BaseModel):
    """One ``promotion_usage`` row. ``promotion`` is null for a legacy orphan (the promotion was deleted),
    ``user`` is null when the account no longer exists; both rows are still listed and counted.
    ``currency`` comes from the order (``promotion_usage.order_id``) and is null for rows without one."""
    id: str
    created_at: Optional[str] = None
    promotion_code: Optional[str] = None
    promotion: Optional[AdminPromotionUsagePromotion] = None
    user: Optional[AdminPromotionUsageUser] = None
    bundle_id: Optional[str] = None
    bundle_code: Optional[str] = None
    order_id: Optional[str] = None
    discount_amount: Optional[float] = None
    currency: Optional[str] = None
    status: Optional[str] = None


class AdminPromotionUsageDiscount(BaseModel):
    currency: Optional[str] = None
    amount: float = 0


class AdminPromotionUsageTotals(BaseModel):
    """Computed in SQL over every row the filters select, never over the page."""
    uses: int = 0
    distinct_users: int = 0
    discount_amount: List[AdminPromotionUsageDiscount] = Field(default_factory=list)


class AdminPromotionUsagePage(AdminPage[AdminPromotionUsageRow]):
    """``totals`` ignore paging: they cover every row matching the filters."""
    totals: Optional[Dict[str, Any]] = None
