from sqlalchemy import Column, String, Integer
from .base import Base


class PromotionRuleEvent(Base):
    __tablename__ = "promotion_rule_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(30), nullable=True)

    def __repr__(self):
        return f"PromotionRuleEvent(id={self.id!r}, name={self.name!r})"
