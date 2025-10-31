from app.models.declare_models.app_config import AppConfig   # SQLAlchemy ORM model
from app.models.declare_models.banner import Banner          # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class ConfigRepo(BaseRepository):
    def __init__(self):
        super().__init__(AppConfig)



class BannerRepo(BaseRepository):
    def __init__(self):
        super().__init__(Banner)
