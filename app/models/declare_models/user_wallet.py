import uuid
from sqlalchemy import Column, Float, DateTime, UUID, func
from .base import Base


class UserWallet(Base):
    __tablename__ = "user_wallet"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    amount = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"UserWallet(id={self.id!r}, amount={self.amount!r})"
