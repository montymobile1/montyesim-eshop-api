import uuid
from sqlalchemy import Column, String, DateTime, ForeignKey, UUID, func, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from .base import Base


class Tag(Base):
    __tablename__ = "tag"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tag_group_id = Column(BigInteger, ForeignKey("tag_group.id"), nullable=True)  # ✅ fixed
    name = Column(String(300), nullable=False)
    data = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"Tag(id={self.id!r}, tag_group_id={self.tag_group_id!r}, name={self.name!r})"
