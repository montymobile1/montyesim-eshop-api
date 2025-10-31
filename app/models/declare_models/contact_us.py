from sqlalchemy import Column, String, DateTime, BigInteger, func
from .base import Base


class ContactUs(Base):
    __tablename__ = "contact_us"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    email = Column(String, nullable=True)
    content = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"ContactUs(id={self.id!r}, email={self.email!r})"
