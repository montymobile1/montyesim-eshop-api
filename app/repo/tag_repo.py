from app.config.db import DatabaseTables
from app.models.app import TagModel, TagTranslationModel
from app.repo.base_repo import BaseRepository


class TagRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_TAG, TagModel)

class TagTranslationRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_TAG_TRANSLATION, TagTranslationModel)
