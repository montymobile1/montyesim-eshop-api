from sqlalchemy import Column, String, Integer
from .base import Base


class PromotionRuleAction(Base):
    __tablename__ = "promotion_rule_action"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(30), nullable=True)

    def __repr__(self):
        return f"PromotionRuleAction(id={self.id!r}, name={self.name!r})"
