from sqlalchemy import Column, String, Text, Boolean, Integer, DateTime, ForeignKey, func, REAL
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class PromotionModel(Base):
    __tablename__ = "promotion"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    rule_id = Column(UUID(as_uuid=True), ForeignKey("promotion_rule.id"), nullable=False)
    code = Column(String(50), unique=True, nullable=False)
    bundle_code = Column(String(200), server_default=None)
    type = Column(String(50), nullable=True)
    amount = Column(REAL, server_default='0')
    callback_url = Column(Text, nullable=True)
    callback_headers = Column(Text, nullable=True)
    valid_from = Column(DateTime(timezone=False), nullable=True)
    valid_to = Column(DateTime(timezone=False), nullable=True)
    is_active = Column(Boolean, server_default='true')
    times_used = Column(Integer, server_default='0')
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    name = Column(String, unique=True, nullable=True)

    def __repr__(self):
        return f"Promotion(id={self.id!r}, code={self.code!r})"
