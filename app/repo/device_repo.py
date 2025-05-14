from app.config.db import DatabaseTables
from app.models.app import DeviceModel
from app.repo.base_repo import BaseRepository


class DeviceRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_DEVICE, DeviceModel)
