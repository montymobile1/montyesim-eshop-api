from app.models.declare_models.tag import Tag                   # SQLAlchemy ORM model
from app.models.declare_models.tag_translation import TagTranslation  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class TagRepo(BaseRepository):
    def __init__(self):
        super().__init__(Tag)


class TagTranslationRepo(BaseRepository):
    def __init__(self):
        super().__init__(TagTranslation)
