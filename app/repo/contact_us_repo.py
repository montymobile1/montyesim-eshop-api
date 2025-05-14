from app.config.db import DatabaseTables
from app.models.app import ContactUsModel
from app.repo.base_repo import BaseRepository


class ContactUsRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_CONTACT_US, ContactUsModel)
