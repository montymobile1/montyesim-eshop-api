import uuid
from sqlalchemy import Column, String, Text, Integer, DateTime, ForeignKey, UUID, func
from .base import Base


class PromotionRule(Base):
    __tablename__ = "promotion_rule"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    promotion_rule_action_id = Column(Integer, ForeignKey("promotion_rule_action.id"))
    promotion_rule_event_id = Column(Integer, ForeignKey("promotion_rule_event.id"))
    max_usage = Column(Integer, nullable=True)
    beneficiary = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    id_text = Column(Text, nullable=True)
    name = Column(Text, nullable=True)

    def __repr__(self):
        return f"PromotionRule(id={self.id!r}, name={self.name!r})"
