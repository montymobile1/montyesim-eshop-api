from sqlalchemy import Column, String, DateTime, BigInteger, func
from .base import Base


class UserOtp(Base):
    __tablename__ = "user_otp"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    email = Column(String(200), nullable=False)
    otp = Column(String(10), nullable=False)
    status = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expired_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"UserOtp(id={self.id!r}, email={self.email!r}, otp={self.otp!r})"
