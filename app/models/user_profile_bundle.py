from sqlalchemy import Column, String, DateTime, Boolean, BigInteger, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .base import Base


class UserProfileBundleModel(Base):
    __tablename__ = "user_profile_bundle"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("auth.users.id"), server_default=func.auth.uid(), nullable=False)
    user_order_id = Column(UUID(as_uuid=True), ForeignKey("user_order.id"), nullable=False)
    plan_started = Column(Boolean, server_default='false', nullable=False)
    bundle_expired = Column(Boolean, server_default='false', nullable=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)
    bundle_type = Column(String(100), server_default='Primary Bundle', nullable=False)
    bundle_data = Column(JSONB, nullable=False)
    iccid = Column(String, nullable=True)
    user_profile_id = Column(UUID(as_uuid=True), ForeignKey("user_profile.id"), nullable=False)
    esim_hub_order_id = Column(String, nullable=True)

    def __repr__(self):
        return f"UserProfileBundle(id={self.id!r}, user_id={self.user_id!r}, bundle_type={self.bundle_type!r})"
