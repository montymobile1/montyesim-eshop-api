from app.config.db import DatabaseTables
from app.models.app import TagModel
from app.repo.base_repo import BaseRepository


class TagGroupRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_TAG_GROUP, TagModel)