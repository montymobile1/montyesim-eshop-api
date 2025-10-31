import uuid
from sqlalchemy import Column, String, DateTime, Boolean, UUID, func
from sqlalchemy.dialects.postgresql import JSONB
from .base import Base


class Bundle(Base):
    __tablename__ = "bundle"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    data = Column(JSONB, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    bundle_name = Column(String(200), nullable=True)

    def __repr__(self):
        return f"Bundle(id={self.id!r}, bundle_name={self.bundle_name!r})"
