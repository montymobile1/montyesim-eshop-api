from app.models.declare_models.contact_us import ContactUs  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class ContactUsRepo(BaseRepository):
    def __init__(self):
        super().__init__(ContactUs)
