import uuid
from sqlalchemy import Column, String, DateTime, Float, UUID, func
from .base import Base


class UserOrder(Base):
    __tablename__ = "user_order"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_profile_id = Column(UUID(as_uuid=True), nullable=False)
    bundle_id = Column(UUID(as_uuid=True), nullable=True)
    amount = Column(Float, nullable=True)
    status = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"UserOrder(id={self.id!r}, amount={self.amount!r}, status={self.status!r})"
