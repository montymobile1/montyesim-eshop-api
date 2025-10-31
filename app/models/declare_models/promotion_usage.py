import uuid
from sqlalchemy import Column, String, DateTime, Float, UUID, func
from .base import Base


class PromotionUsage(Base):
    __tablename__ = "promotion_usage"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    promotion_code = Column(String(100), nullable=True)
    referral_code = Column(String(100), nullable=True)
    amount = Column(Float, nullable=True)
    status = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    bundle_id = Column(UUID(as_uuid=True), nullable=True)
    device_id = Column(String(250), nullable=True)
    referred_to = Column(String(200), nullable=True)
    order_id = Column(UUID(as_uuid=True), nullable=True)

    def __repr__(self):
        return (
            f"PromotionUsage(id={self.id!r}, user_id={self.user_id!r}, "
            f"promotion_code={self.promotion_code!r}, status={self.status!r}, amount={self.amount!r})"
        )
