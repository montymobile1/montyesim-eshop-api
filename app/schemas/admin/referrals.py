"""Request and response shapes of /api/v1/admin/referrals (MS-5056)."""
from datetime import date as Date  # the model field is called `date`
from typing import Optional
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

MAX_SEARCH_LENGTH = 100
MAX_EMAIL_LENGTH = 255


class AdminReferralFilters(BaseModel):
    """The four filters shared by the list and the export (same meaning as the legacy RPCs)."""
    user_id: Optional[UUID] = None          # the referred user (user_order.user_id)
    search: Optional[str] = None            # part of the referral code
    from_email: Optional[str] = None        # part of the referred user's email (promotion_usage.referred_to)
    date: Optional[Date] = None             # referral day (user_order.created_at)

    model_config = ConfigDict(extra="forbid")

    def rpc_params(self) -> dict:
        """The parameters of ``admin_referral_transactions`` (blank texts become NULL)."""
        return {
            "p_user_id": str(self.user_id) if self.user_id else None,
            "p_search": _clean(self.search),
            "p_from_email": _clean(self.from_email),
            "p_date": self.date.isoformat() if self.date else None,
        }

    def audit_filters(self) -> dict:
        """The filters as stored in the EXPORT audit row (only the ones that were set)."""
        return {key: value for key, value in {
            "user_id": str(self.user_id) if self.user_id else None,
            "search": _clean(self.search),
            "from_email": _clean(self.from_email),
            "date": self.date.isoformat() if self.date else None,
        }.items() if value is not None}


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = text.strip()
    return text or None


def referral_filters(user_id: Optional[UUID] = Query(None, description="the referred user's id"),
                     search: Optional[str] = Query(None, max_length=MAX_SEARCH_LENGTH,
                                                   description="part of the referral code"),
                     from_email: Optional[str] = Query(None, max_length=MAX_EMAIL_LENGTH,
                                                       description="part of the referred user's email"),
                     date: Optional[Date] = Query(None, description="referral day, YYYY-MM-DD")) -> AdminReferralFilters:
    """Dependency for ``GET /referrals``: the filters as query parameters."""
    return AdminReferralFilters(user_id=user_id, search=search, from_email=from_email, date=date)


class AdminReferralExportRequest(AdminReferralFilters):
    """Body of ``POST /referrals/export``: the same filters as the list, all optional."""
    search: Optional[str] = Field(None, max_length=MAX_SEARCH_LENGTH)
    from_email: Optional[str] = Field(None, max_length=MAX_EMAIL_LENGTH)


class AdminReferralUser(BaseModel):
    id: Optional[str] = None
    email: Optional[str] = None


class AdminReferral(BaseModel):
    """One referral transaction: a successful order paid with a referral code and its reward."""
    id: Optional[str] = None
    created_at: Optional[str] = None
    payment_time: Optional[str] = None
    referral_code: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    modified_amount: Optional[float] = None
    order_status: Optional[str] = None
    status: Optional[str] = None
    referrer: Optional[AdminReferralUser] = None   # owner of the code, receives the reward ("To User")
    referred: Optional[AdminReferralUser] = None   # bought with the code ("From User")
