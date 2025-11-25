from sqlalchemy import Column, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .base import Base


class UsersCopyModel(Base):
    __tablename__ = "users_copy"

    id = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    email = Column(Text, unique=True, nullable=True)
    metadata_json = Column("metadata", JSONB, nullable=True)  # Map to 'metadata' column in DB

    def __repr__(self):
        return f"UsersCopy(id={self.id!r}, email={self.email!r})"
