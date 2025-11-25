from sqlalchemy import Column, Text, Integer, DateTime, ForeignKey, func, Computed
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class PromotionRuleModel(Base):
    __tablename__ = "promotion_rule"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    promotion_rule_action_id = Column(Integer, ForeignKey("promotion_rule_action.id"), nullable=False)
    promotion_rule_event_id = Column(Integer, ForeignKey("promotion_rule_event.id"), nullable=False)
    max_usage = Column(Integer, server_default='1', nullable=False)
    beneficiary = Column(Integer, server_default='0')
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    id_text = Column(Text, Computed("id::text"), nullable=True)
    name = Column(Text, server_default='')

    def __repr__(self):
        return f"PromotionRule(id={self.id!r}, name={self.name!r})"
