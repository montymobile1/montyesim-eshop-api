from sqlalchemy import Column, String, JSON, BigInteger
from .base import Base


class UsersCopy(Base):
    __tablename__ = "users_copy"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    email = Column(String(200), nullable=True)
    metadata_json = Column("metadata", JSON, nullable=True)  # alias

    def __repr__(self):
        return f"UsersCopy(id={self.id!r}, email={self.email!r})"
