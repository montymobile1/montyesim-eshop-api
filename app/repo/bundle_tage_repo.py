from app.config.db import DatabaseTables
from app.models.app import BundleTagModel
from app.repo.base_repo import BaseRepository


class BundleTagRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BUNDLE_TAG, BundleTagModel)
