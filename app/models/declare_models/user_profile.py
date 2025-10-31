import uuid
from sqlalchemy import Column, String, DateTime, UUID, func
from .base import Base


class UserProfile(Base):
    __tablename__ = "user_profile"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String(200), nullable=True)
    email = Column(String(200), nullable=True)
    mobile = Column(String(20), nullable=True)
    country = Column(String(100), nullable=True)
    address = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"UserProfile(id={self.id!r}, email={self.email!r})"
