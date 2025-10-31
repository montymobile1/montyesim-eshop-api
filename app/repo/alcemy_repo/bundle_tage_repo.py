from app.models.declare_models.bundle_tag import BundleTag  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class BundleTagRepo(BaseRepository):
    def __init__(self):
        super().__init__(BundleTag)
