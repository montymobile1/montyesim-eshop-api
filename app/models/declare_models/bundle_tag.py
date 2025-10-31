from sqlalchemy import Column, DateTime, ForeignKey, BigInteger, func
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class BundleTag(Base):
    __tablename__ = "bundle_tag"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    bundle_id = Column(UUID(as_uuid=True), ForeignKey("bundle.id"))
    tag_id = Column(UUID(as_uuid=True), ForeignKey("tag.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"BundleTag(id={self.id!r}, bundle_id={self.bundle_id!r}, tag_id={self.tag_id!r})"
