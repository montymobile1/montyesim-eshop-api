from sqlalchemy import Column, String, Boolean, DateTime, BigInteger, func
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class DeviceModel(Base):
    __tablename__ = "device"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    device_id = Column(String, nullable=False)
    fcm_token = Column(String, nullable=True)
    manufacturer = Column(String, nullable=True)
    device_model = Column(String, nullable=True)
    os = Column(String, nullable=True)
    os_version = Column(String, nullable=True)
    app_version = Column(String, nullable=True)
    ram_size = Column(String, nullable=True)
    screen_resolution = Column(String, nullable=True)
    is_rooted = Column(Boolean, nullable=True)
    is_logged_in = Column(Boolean, nullable=False)
    originated_ip = Column(String, nullable=True)
    ip_location = Column(String, nullable=True)
    timestamp_login = Column(DateTime(timezone=False), nullable=True)
    timestamp_logout = Column(DateTime(timezone=False), nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"Device(id={self.id!r}, user_id={self.user_id!r}, device_id={self.device_id!r})"
