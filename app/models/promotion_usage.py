from sqlalchemy import Column, String, DateTime, ForeignKey, func, REAL
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class PromotionUsageModel(Base):
    __tablename__ = "promotion_usage"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    promotion_code = Column(String, ForeignKey("promotion.code"), nullable=True)
    referral_code = Column(String, nullable=True)
    amount = Column(REAL, server_default='0')
    status = Column(String(20), server_default='pending', nullable=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    bundle_id = Column(UUID(as_uuid=True), nullable=True)
    device_id = Column(String(250), nullable=True)
    referred_to = Column(String, nullable=True)
    order_id = Column(UUID(as_uuid=True), nullable=True)

    def __repr__(self):
        return (
            f"PromotionUsage(id={self.id!r}, user_id={self.user_id!r}, "
            f"promotion_code={self.promotion_code!r}, status={self.status!r}, amount={self.amount!r})"
        )
