from app.models.declare_models.device import Device  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class DeviceRepo(BaseRepository):
    def __init__(self):
        super().__init__(Device)
