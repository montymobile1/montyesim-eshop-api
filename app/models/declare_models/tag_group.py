from sqlalchemy import Column, String, Boolean, Integer, DateTime, BigInteger, func
from .base import Base


class TagGroup(Base):
    __tablename__ = "tag_group"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(300), nullable=False)
    type = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    group_category = Column(String(200), nullable=True)

    def __repr__(self):
        return f"TagGroup(id={self.id!r}, name={self.name!r})"
