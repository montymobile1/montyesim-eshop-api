from sqlalchemy import Column, String, Text, DateTime, BigInteger, func
from .base import Base


class AppConfigModel(Base):
    __tablename__ = "app_config"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"AppConfig(id={self.id!r}, key={self.key!r}, value={self.value!r}, created_at={self.created_at!r})"
