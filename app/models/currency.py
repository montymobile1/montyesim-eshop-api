from sqlalchemy import Column, String, DateTime, BigInteger, func, REAL
from .base import Base


class CurrencyModel(Base):
    __tablename__ = "currency"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(300), nullable=True)
    rate = Column(REAL, nullable=False)
    default_currency = Column(String(300), nullable=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())

    def __repr__(self):
        return f"Currency(id={self.id!r}, name={self.name!r}, rate={self.rate!r})"
