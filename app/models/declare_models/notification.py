from sqlalchemy import Column, String, Text, DateTime, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class Notification(Base):
    __tablename__ = "notification"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True))
    title = Column(String(200), nullable=True)
    content = Column(Text, nullable=True)
    status = Column(String(50), nullable=True)
    updated_at = Column(DateTime(timezone=True))
    user_id = Column(UUID(as_uuid=True), nullable=False)
    image_url = Column(String(300), nullable=True)

    def __repr__(self):
        return f"Notification(id={self.id!r}, title={self.title!r})"
