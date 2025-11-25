from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class UserWalletTransactionModel(Base):
    __tablename__ = "user_wallet_transaction"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("user_wallet.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric, nullable=False)
    status = Column(String(10), nullable=False)
    source = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())

    def __repr__(self):
        return f"UserWalletTransaction(id={self.id!r}, amount={self.amount!r}, status={self.status!r})"
