from sqlalchemy import Column, String, DateTime, ForeignKey, BigInteger, func
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .base import Base


class TagModel(Base):
    __tablename__ = "tag"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    tag_group_id = Column(BigInteger, ForeignKey("tag_group.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(300), nullable=False)
    icon = Column(String(300), nullable=True)
    data = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())

    # Relationship to BundleTag
    bundle_tags = relationship("BundleTagModel", back_populates="tag", lazy="select")

    def __repr__(self):
        return f"Tag(id={self.id!r}, tag_group_id={self.tag_group_id!r}, name={self.name!r})"
