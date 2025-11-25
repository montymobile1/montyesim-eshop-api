from sqlalchemy import Column, String, DateTime, BigInteger, func
from .base import Base


class ContactUsModel(Base):
    __tablename__ = "contact_us"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    email = Column(String, nullable=True)
    content = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"ContactUs(id={self.id!r}, email={self.email!r})"
