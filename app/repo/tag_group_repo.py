from app.models.tag import TagModel
from app.repo.base_repo import BaseRepository


class TagGroupRepo(BaseRepository):

    def __init__(self):
        super().__init__(TagModel)
