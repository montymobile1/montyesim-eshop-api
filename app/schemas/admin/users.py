"""Request and response shapes of /api/v1/admin/users (MS-5079)."""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

UserBanStatus = Literal["all", "banned", "active"]
#: Sort columns of the users list (TEOS-111); anything else is a 422.
UserSort = Literal["created_at", "email"]
SortDirection = Literal["asc", "desc"]

#: 365 days, what the portal pre-fills (TEOS-63).
DEFAULT_BAN_HOURS = 24 * 365
#: 10 years.
MAX_BAN_HOURS = 87_600
MAX_REASON_LENGTH = 500


def _non_blank(value: str) -> str:
    value = value.strip() if isinstance(value, str) else value
    if not value:
        raise ValueError("reason must not be blank")
    return value


class AdminUserBan(BaseModel):
    """Body of ``POST /users/{id}/ban``."""
    hours: int = Field(DEFAULT_BAN_HOURS, ge=1, le=MAX_BAN_HOURS)
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)

    model_config = ConfigDict(extra="forbid")

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        return _non_blank(value)


class AdminUserUnban(BaseModel):
    """Body of ``POST /users/{id}/unban``."""
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_LENGTH)

    model_config = ConfigDict(extra="forbid")

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        return _non_blank(value)


class AdminUserRow(BaseModel):
    """One row of the users list (``admin_search_users``)."""
    id: str
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    msisdn: Optional[str] = None
    referral_code: Optional[str] = None
    email_verified: Optional[bool] = None
    phone_verified: Optional[bool] = None
    should_notify: Optional[bool] = None
    role: Optional[str] = None
    banned_until: Optional[str] = None
    is_banned: bool = False
    created_at: Optional[str] = None
    last_sign_in_at: Optional[str] = None


class AdminUserWallet(BaseModel):
    amount: float = 0
    currency: Optional[str] = None


class AdminUserDetail(AdminUserRow):
    """``GET /users/{id}``: the row plus the wallet (``None`` when the user has no wallet row)."""
    wallet: Optional[AdminUserWallet] = None


class AdminUserBanResult(BaseModel):
    user_id: str
    email: Optional[str] = None
    banned_until: Optional[str] = None
    hours_banned: int


class AdminUserUnbanResult(BaseModel):
    user_id: str
    email: Optional[str] = None


class AdminWalletTransaction(BaseModel):
    """One row of ``GET /users/{id}/wallet-transactions`` (``admin_wallet_transactions``)."""
    id: str
    wallet_id: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None
    source: Optional[str] = None
    currency: Optional[str] = None
    created_at: Optional[str] = None


class AdminUserReferral(BaseModel):
    """One row of ``GET /users/{id}/referrals``: whatever ``admin_referral_transactions`` returns
    (the shape is owned by the Referrals story, MS-5056)."""
    model_config = ConfigDict(extra="allow")


__all__: List[str] = [
    "UserBanStatus", "UserSort", "SortDirection", "DEFAULT_BAN_HOURS", "MAX_BAN_HOURS", "AdminUserBan", "AdminUserUnban",
    "AdminUserRow", "AdminUserWallet", "AdminUserDetail", "AdminUserBanResult", "AdminUserUnbanResult",
    "AdminWalletTransaction", "AdminUserReferral",
]
