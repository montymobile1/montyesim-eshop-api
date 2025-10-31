from sqlalchemy import Column, String, DateTime, BigInteger, func
from .base import Base


class Banner(Base):
    __tablename__ = "banner"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    title = Column(String(300), nullable=True)
    description = Column(String(300), nullable=True)
    action = Column(String(100), nullable=True)   # e.g., CHAT, REFER_NOW, CASHBACK
    image = Column(String(300), nullable=True)
    platform = Column(String(20), nullable=True)  # e.g., web, mobile
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"Banner(id={self.id!r}, title={self.title!r})"
