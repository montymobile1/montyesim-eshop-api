from app.models.notification import NotificationModel
from app.repo.base_repo import BaseRepository


class NotificationRepo(BaseRepository):

    def __init__(self):
        super().__init__(NotificationModel)
