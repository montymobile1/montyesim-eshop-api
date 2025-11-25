from sqlalchemy import Column, String, DateTime, BigInteger, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .base import Base


class TagTranslationModel(Base):
    __tablename__ = "tag_translation"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tag_id = Column(UUID(as_uuid=True), nullable=True)
    locale = Column(String(30), server_default='en', nullable=False)
    name = Column(String(500), nullable=False)
    data = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())

    def __repr__(self):
        return f"TagTranslation(id={self.id!r}, locale={self.locale!r}, name={self.name!r})"
