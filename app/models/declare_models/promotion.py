import uuid
from sqlalchemy import Column, String, Text, Float, Boolean, Integer, DateTime, ForeignKey, UUID, func
from .base import Base


class Promotion(Base):
    __tablename__ = "promotion"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id = Column(UUID(as_uuid=True), ForeignKey("promotion_rule.id"))
    code = Column(String(50), nullable=True)
    bundle_code = Column(String(200), nullable=True)
    type = Column(String(50), nullable=True)
    amount = Column(Float, nullable=True)
    callback_url = Column(Text, nullable=True)
    callback_headers = Column(Text, nullable=True)
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    times_used = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    name = Column(String, nullable=True)

    def __repr__(self):
        return f"Promotion(id={self.id!r}, code={self.code!r})"
