from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from .base import Base


class UserProfileModel(Base):
    __tablename__ = "user_profile"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("auth.users.id"), server_default=func.auth.uid())
    user_order_id = Column(UUID(as_uuid=True), ForeignKey("user_order.id"), nullable=False)
    iccid = Column(String(100), nullable=False)
    smdp_address = Column(String(100), nullable=False)
    validity = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    label = Column(String(200), nullable=True)
    activation_code = Column(String(200), nullable=False)
    allow_topup = Column(Boolean, server_default='false', nullable=False)
    esim_hub_order_id = Column(String(200), nullable=True)
    searched_countries = Column(String(200), nullable=True)
    shared_user_id = Column(UUID(as_uuid=True), server_default=func.gen_random_uuid())

    order = relationship("UserOrderModel", back_populates="user_profile")
    user_profile_bundle = relationship("UserProfileBundleModel", back_populates="user_profile")

    def __repr__(self):
        return f"UserProfile(id={self.id!r}, iccid={self.iccid!r})"
