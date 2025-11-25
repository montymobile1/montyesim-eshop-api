from sqlalchemy import Column, String, Text, DateTime, BigInteger, Boolean, func
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class NotificationModel(Base):
    __tablename__ = "notification"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)
    title = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    status = Column(Boolean, nullable=True)
    updated_at = Column(DateTime(timezone=False), server_default=func.now())
    data = Column(Text, nullable=True)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    image_url = Column(String, nullable=True)

    def __repr__(self):
        return f"Notification(id={self.id!r}, title={self.title!r})"
