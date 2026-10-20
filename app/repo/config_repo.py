from typing import List

from app.config.db import DatabaseTables
from app.exceptions import DatabaseException
from app.models.app import AppConfigModel
from app.models.app import BannerModel
from app.repo.base_repo import BaseRepository


class ConfigRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_APP_CONFIG, AppConfigModel)

    def insert_if_absent(self, key: str, value) -> None:
        """Insert the setting unless a row with that key exists (key is unique): concurrent inserts keep one row."""
        try:
            self.table.upsert({"key": key, "value": value}, on_conflict="key", ignore_duplicates=True).execute()
        except Exception as e:
            raise DatabaseException(str(e))


class BannerRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BANNER, BannerModel)

    def get_banners(self,platform: str, locale: str = "en") -> List[BannerModel]:
        return super().select_procedure(where={"p_platform": platform,"p_locale": locale},
                                        function_name="get_banners")



