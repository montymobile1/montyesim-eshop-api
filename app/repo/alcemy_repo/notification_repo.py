from app.models.declare_models.notification import Notification  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class NotificationRepo(BaseRepository):
    def __init__(self):
        super().__init__(Notification)
