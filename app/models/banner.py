from sqlalchemy import Column, String, DateTime, Integer, func
from .base import Base


class BannerModel(Base):
    __tablename__ = "banner"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=True)
    description = Column(String, nullable=True)
    action = Column(String, nullable=True)
    image = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.current_timestamp())
    platform = Column(String, nullable=True)

    def __repr__(self):
        return f"Banner(id={self.id!r}, title={self.title!r})"
