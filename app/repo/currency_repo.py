from app.models.currency import CurrencyModel
from app.repo.base_repo import BaseRepository


class CurrencyRepo(BaseRepository):

    def __init__(self):
        super().__init__(CurrencyModel)
