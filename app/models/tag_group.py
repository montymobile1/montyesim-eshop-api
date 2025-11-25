from sqlalchemy import Column, String, Boolean, Integer, DateTime, BigInteger, func
from .base import Base


class TagGroupModel(Base):
    __tablename__ = "tag_group"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(300), unique=True, nullable=True)
    type = Column(Integer, nullable=False)
    is_active = Column(Boolean, server_default='true')
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now())
    group_category = Column(String, nullable=True)

    def __repr__(self):
        return f"TagGroup(id={self.id!r}, name={self.name!r})"
