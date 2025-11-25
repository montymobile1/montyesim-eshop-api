from sqlalchemy import Column, String, DateTime, BigInteger, Boolean, func, REAL
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class VoucherModel(Base):
    __tablename__ = "voucher"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(300), unique=True, nullable=True)
    amount = Column(REAL, nullable=False)
    is_used = Column(Boolean, server_default='false', nullable=False)
    is_active = Column(Boolean, server_default='true', nullable=False)
    used_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())
    expired_at = Column(DateTime(timezone=False), nullable=True)

    def __repr__(self):
        return f"Voucher(id={self.id!r}, code={self.code!r}, amount={self.amount!r})"
