from app.models.device import DeviceModel
from app.repo.base_repo import BaseRepository


class DeviceRepo(BaseRepository):
    def __init__(self):
        super().__init__(DeviceModel)
