from sqlalchemy import Column, String, SmallInteger
from .base import Base


class PromotionRuleEventModel(Base):
    __tablename__ = "promotion_rule_event"

    id = Column(SmallInteger, primary_key=True, nullable=False)
    name = Column(String(30), nullable=True)

    def __repr__(self):
        return f"PromotionRuleEvent(id={self.id!r}, name={self.name!r})"
