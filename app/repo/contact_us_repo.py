from app.models.contact_us import ContactUsModel
from app.repo.base_repo import BaseRepository


class ContactUsRepo(BaseRepository):

    def __init__(self):
        super().__init__(ContactUsModel)
