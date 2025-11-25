from app.models.app_config import AppConfigModel
from app.models.banner import BannerModel
from app.repo.base_repo import BaseRepository


class ConfigRepo(BaseRepository):
    def __init__(self):
        super().__init__(AppConfigModel)


class BannerRepo(BaseRepository):
    def __init__(self):
        super().__init__(BannerModel)
