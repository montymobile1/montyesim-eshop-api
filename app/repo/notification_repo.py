from app.config.db import DatabaseTables
from app.models.notification import NotificationModel
from app.repo.base_repo import BaseRepository


class NotificationRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_NOTIFICATION, NotificationModel)
