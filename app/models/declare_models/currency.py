from sqlalchemy import Column, String, Float, DateTime, BigInteger, func
from .base import Base


class Currency(Base):
    __tablename__ = "currency"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(300), nullable=False)
    rate = Column(Float, nullable=False)
    default_currency = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"Currency(id={self.id!r}, name={self.name!r}, rate={self.rate!r})"
