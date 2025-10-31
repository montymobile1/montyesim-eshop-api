import uuid
from sqlalchemy import Column, String, Text, DateTime, UUID, func
from .base import Base


class AppConfig(Base):
    __tablename__ = "app_config"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"AppConfig(id={self.id!r}, key={self.key!r})"
