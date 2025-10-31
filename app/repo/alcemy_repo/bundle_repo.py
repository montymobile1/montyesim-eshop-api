from app.models.declare_models.bundle import Bundle  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class BundleRepo(BaseRepository):

    def __init__(self):
        # Pass the ORM model + Pydantic DTO
        super().__init__(Bundle)
