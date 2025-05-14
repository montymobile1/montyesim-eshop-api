from typing import Optional

from pydantic import BaseModel, Field

from app.config.db import PromotionStatusEnum


class PromotionRuleActionModel(BaseModel):
    id: int = Field(None, alias="id")
    name: str = Field(None, alias="name")


class PromotionRuleEventModel(BaseModel):
    id: int = Field(None, alias="id")
    name: str = Field(None, alias="name")


class PromotionRuleModel(BaseModel):
    id: Optional[str] = Field(None, alias="id")
    promotion_rule_action_id: int = Field(None, alias="promotion_rule_action_id")
    promotion_rule_event_id: int = Field(None, alias="promotion_rule_event_id")
    max_usage: int = Field(None, alias="max_usage")
    beneficiary: int = Field(None, alias="beneficiary", description="who will benefit from the rule")
    created_at: str = Field(None, alias="created_at")
    promotion_rule_action: Optional[PromotionRuleActionModel] = Field(None, alias="promotion_rule_action")
    promotion_rule_event: Optional[PromotionRuleEventModel] = Field(None, alias="promotion_rule_event")


class PromotionModel(BaseModel):
    id: Optional[str] = Field(None, alias="id")
    rule_id: str = Field(None, alias="rule_id")
    code: str = Field(None, alias="code")
    bundle_code: Optional[str] = Field(None, alias="bundle_code")
    type: str = Field(None, alias="type")
    amount: float = Field(None, alias="amount")
    name: Optional[str] = Field(None, alias="name")
    callback_url: Optional[str] = Field(None, alias="callback_url")
    callback_headers: Optional[str] = Field(None, alias="callback_headers")
    valid_from: str = Field(None, alias="valid_from")
    valid_to: str = Field(None, alias="valid_to")
    is_active: bool = Field(None, alias="is_active")
    times_used: int = Field(None, alias="times_used")
    created_at: str = Field(None, alias="created_at")
    promotion_rule: Optional[PromotionRuleModel] = Field(None, alias="promotion_rule")


class PromotionUsageModel(BaseModel):
    id: Optional[str] = Field(None, alias="id")
    user_id: str = Field(None, alias="user_id")
    promotion_code: Optional[str] = Field(None, alias="promotion_code")
    referral_code: Optional[str] = Field(None, alias="referral_code")
    amount: float = Field(None, alias="amount")
    bundle_id: Optional[str] = Field(None, alias="bundle_id")
    status: PromotionStatusEnum = Field(PromotionStatusEnum.PENDING, alias="status")
    created_at: Optional[str] = Field(None, alias="created_at")
