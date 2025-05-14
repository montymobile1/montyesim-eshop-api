from app.config.db import DatabaseTables
from app.models.app import AppConfigModel
from app.repo.base_repo import BaseRepository


class ConfigRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_APP_CONFIG, AppConfigModel)
