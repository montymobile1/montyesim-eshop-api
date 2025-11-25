from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey, func, DECIMAL
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class UserOrderModel(Base):
    __tablename__ = "user_order"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("auth.users.id"), server_default=func.auth.uid(), nullable=False)
    esim_order_id = Column(String, nullable=True)
    bundle_id = Column(String, nullable=True)
    amount = Column(Numeric, nullable=False)
    currency = Column(String, nullable=False)
    order_type = Column(String, server_default='assign', nullable=False)
    payment_status = Column(String, server_default='pending', nullable=False)
    order_status = Column(String, server_default='pending', nullable=False)
    payment_time = Column(DateTime(timezone=False), nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)
    callback_time = Column(DateTime(timezone=False), nullable=True)
    bundle_data = Column(String, nullable=True)
    searched_countries = Column(String, nullable=True)
    anonymous_user_id = Column(UUID(as_uuid=True), ForeignKey("auth.users.id"), server_default=func.auth.uid())
    payment_intent_code = Column(String, nullable=True)
    promo_code = Column(String(50), ForeignKey("promotion.code"), server_default=None)
    referral_code = Column(String(50), server_default=None)
    modified_amount = Column(DECIMAL, nullable=True)
    otp = Column(String, nullable=True)
    payment_type = Column(String(100), server_default=None)
    otp_expired_at = Column(DateTime(timezone=False), server_default=None)
    tax_amount = Column(Numeric, server_default='0')

    def __repr__(self):
        return f"UserOrder(id={self.id!r}, amount={self.amount!r}, status={self.status!r})"
