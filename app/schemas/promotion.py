from typing import Optional, Generic, TypeVar
from datetime import datetime

from pydantic import BaseModel, ConfigDict ,field_validator

class PromotionCodeDetailsResponse(BaseModel):
    code_type: str
    rule_id: str

class PromotionValidationRequest(BaseModel):
    promo_code: str
    bundle_code: str

class PromotionCheck(BaseModel):
    amount: float
    message: str

class ReferralRewardRequest(BaseModel):
    referral_code: str

class PromotionHistoryDto(BaseModel):
    is_referral: bool
    amount: str
    name: Optional[str]
    promotion_name: Optional[str]
    date: str

    @field_validator("date", mode="before")
    @classmethod
    def convert_to_timestamp(cls, value):
        if value is None:
            return None
        dt = datetime.fromisoformat(value)
        return str(int(dt.timestamp()))

