from sqlalchemy import Column, String, DateTime, Integer, Boolean, func
from .base import Base


class UserOtpModel(Base):
    __tablename__ = "user_otp"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(100), nullable=True)
    mobile = Column(String(100), nullable=True)
    otp = Column(String(6), nullable=False)
    expire_at = Column(DateTime(timezone=False), nullable=True)
    is_used = Column(Boolean, server_default='false')
    created_at = Column(DateTime(timezone=False), server_default=func.current_timestamp())

    def __repr__(self):
        return f"UserOtp(id={self.id!r}, email={self.email!r}, otp={self.otp!r})"
