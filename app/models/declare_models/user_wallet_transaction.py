import uuid
from sqlalchemy import Column, String, Float, DateTime, UUID, func
from .base import Base


class UserWalletTransaction(Base):
    __tablename__ = "user_wallet_transaction"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id = Column(UUID(as_uuid=True), nullable=False)
    amount = Column(Float, nullable=True)
    status = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"UserWalletTransaction(id={self.id!r}, amount={self.amount!r}, status={self.status!r})"
