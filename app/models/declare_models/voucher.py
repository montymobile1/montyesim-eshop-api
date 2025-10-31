from sqlalchemy import Column, String, Float, DateTime, BigInteger, func
from .base import Base


class Voucher(Base):
    __tablename__ = "voucher"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(100), nullable=False)
    amount = Column(Float, nullable=True)
    status = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    expired_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"Voucher(id={self.id!r}, code={self.code!r}, amount={self.amount!r})"
