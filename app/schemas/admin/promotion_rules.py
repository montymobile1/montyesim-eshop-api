"""Request and response shapes of /api/v1/admin/promotion-rules (MS-5087)."""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt

MAX_USAGE_LIMIT = 2_147_483_647  # integer column


class AdminPromotionRuleBody(BaseModel):
    """Body of ``POST /promotion-rules`` and ``PUT /promotion-rules/{id}``.

    ``beneficiary``: 0 (none), 1 (sender), 2 (sender and receiver), as the ``promotion_rule_beneficiary_check``
    constraint allows. The rule name (``<action>-<event>-<max_usage>``) is filled by the database trigger.
    ``max_usage`` must be a JSON integer: "5" is a 422 instead of being converted (TEOS-211).
    """
    promotion_rule_action_id: int = Field(..., ge=1)
    promotion_rule_event_id: int = Field(..., ge=1)
    max_usage: StrictInt = Field(..., ge=1, le=MAX_USAGE_LIMIT)
    beneficiary: int = Field(..., ge=0, le=2)

    model_config = ConfigDict(extra="forbid")


class AdminPromotionRuleRef(BaseModel):
    """An action (``promotion_rule_action``) or an event (``promotion_rule_event``)."""
    id: int
    name: Optional[str] = None


class AdminPromotionRule(BaseModel):
    """One row of the rules list (and the create / update response). ``action`` / ``event`` are joined
    server-side; ``promotion_count``: the promotions using the rule (TEOS-210)."""
    id: str
    id_text: Optional[str] = None
    name: Optional[str] = None
    max_usage: Optional[int] = None
    beneficiary: Optional[int] = None
    created_at: Optional[str] = None
    action: Optional[AdminPromotionRuleRef] = None
    event: Optional[AdminPromotionRuleRef] = None
    promotion_count: int = 0
