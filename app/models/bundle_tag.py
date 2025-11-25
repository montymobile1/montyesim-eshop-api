from sqlalchemy import Column, DateTime, ForeignKey, BigInteger, Boolean, func
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class BundleTagModel(Base):
    __tablename__ = "bundle_tag"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    bundle_id = Column(UUID(as_uuid=True), ForeignKey("bundle.id"), nullable=False)
    tag_id = Column(UUID(as_uuid=True), ForeignKey("tag.id"), nullable=False)
    is_active = Column(Boolean, server_default='true')
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())

    # Relationships
    bundle = relationship("BundleModel", back_populates="bundle_tags", lazy="select")
    tag = relationship("TagModel", back_populates="bundle_tags", lazy="select")

    def __repr__(self):
        return f"BundleTag(id={self.id!r}, bundle_id={self.bundle_id!r}, tag_id={self.tag_id!r})"
