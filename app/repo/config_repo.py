from typing import List

from app.config.db import DatabaseTables
from app.models.app import AppConfigModel
from app.models.app import BannerModel
from app.repo.base_repo import BaseRepository


class ConfigRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_APP_CONFIG, AppConfigModel)


class BannerRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BANNER, BannerModel)

    def get_banners(self,platform: str, locale: str = "en") -> List[BannerModel]:
        return super().select_procedure(where={"p_platform": platform,"p_locale": locale},
                                        function_name="get_banners_by_platform_and_locale")



