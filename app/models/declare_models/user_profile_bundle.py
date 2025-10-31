import uuid
from sqlalchemy import Column, String, DateTime, Boolean, UUID, JSON, BigInteger, func
from .base import Base


class UserProfileBundle(Base):
    __tablename__ = "user_profile_bundle"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    user_order_id = Column(UUID(as_uuid=True), nullable=True)
    plan_started = Column(Boolean, default=False)
    bundle_expired = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    bundle_type = Column(String(100), nullable=True)
    bundle_data = Column(JSON, nullable=True)
    iccid = Column(String(200), nullable=True)
    user_profile_id = Column(UUID(as_uuid=True), nullable=True)
    esim_hub_order_id = Column(String(200), nullable=True)

    def __repr__(self):
        return f"UserProfileBundle(id={self.id!r}, user_id={self.user_id!r}, bundle_type={self.bundle_type!r})"
