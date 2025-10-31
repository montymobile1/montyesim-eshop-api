from sqlalchemy import Column, String, DateTime, ForeignKey, BigInteger, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .base import Base


class TagTranslation(Base):
    __tablename__ = "tag_translation"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tag_id = Column(UUID(as_uuid=True), ForeignKey("tag.id"))
    locale = Column(String(50), nullable=False)
    name = Column(String(300), nullable=True)
    data = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"TagTranslation(id={self.id!r}, locale={self.locale!r}, name={self.name!r})"
