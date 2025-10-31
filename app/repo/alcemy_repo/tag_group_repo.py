from app.models.declare_models.tag_group import TagGroup  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class TagGroupRepo(BaseRepository):
    def __init__(self):
        super().__init__(TagGroup)
