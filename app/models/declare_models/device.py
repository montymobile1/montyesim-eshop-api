from sqlalchemy import Column, String, Boolean, DateTime, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class Device(Base):
    __tablename__ = "device"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    device_id = Column(String(200), nullable=True)
    fcm_token = Column(String(300), nullable=True)
    manufacturer = Column(String(100), nullable=True)
    device_model = Column(String(100), nullable=True)
    os_version = Column(String(50), nullable=True)
    app_version = Column(String(50), nullable=True)
    ram_size = Column(String(50), nullable=True)
    screen_resolution = Column(String(100), nullable=True)
    is_rooted = Column(Boolean, default=False)
    is_logged_in = Column(Boolean, default=False)
    originated_ip = Column(String(100), nullable=True)
    ip_location = Column(String(200), nullable=True)
    timestamp_login = Column(DateTime(timezone=True), nullable=True)
    timestamp_logout = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))

    def __repr__(self):
        return f"Device(id={self.id!r}, user_id={self.user_id!r}, device_id={self.device_id!r})"
