from sqlalchemy import Column, Text, DateTime, Boolean, func
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .base import Base


class BundleModel(Base):
    __tablename__ = "bundle"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    data = Column(JSONB, nullable=True)
    is_active = Column(Boolean, server_default='true')
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())
    bundle_name = Column(Text, nullable=True)

    # Relationship to BundleTag
    bundle_tags = relationship("BundleTagModel", back_populates="bundle", lazy="select")

    def __repr__(self):
        return f"Bundle(id={self.id!r}, bundle_name={self.bundle_name!r})"
