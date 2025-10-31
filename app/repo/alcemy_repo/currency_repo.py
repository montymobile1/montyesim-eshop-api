from app.models.declare_models.currency import Currency  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class CurrencyRepo(BaseRepository):
    def __init__(self):
        super().__init__(Currency)
