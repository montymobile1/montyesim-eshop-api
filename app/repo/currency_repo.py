from app.config.db import DatabaseTables
from app.models.app import CurrencyModel
from app.repo.base_repo import BaseRepository


class CurrencyRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_CURRENCY, CurrencyModel)