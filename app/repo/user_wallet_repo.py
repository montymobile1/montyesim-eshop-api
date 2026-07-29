from typing import List

from app.config.db import DatabaseTables
from app.exceptions import DatabaseException
from app.models.user import UserWalletModel, UserWalletTransactionModel
from app.repo.base_repo import BaseRepository


class UserWalletRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET, UserWalletModel)


class UserWalletTransactionRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET_TRANSACTION, UserWalletTransactionModel)

    def list_since(self, where: dict, since: str, limit: int = 1000) -> List[UserWalletTransactionModel]:
        try:
            query = self.table.select("*")
            for key, value in where.items():
                query = query.eq(key, value)
            query = query.gte("created_at", since).order("created_at", desc=False).limit(limit)
            response = query.execute()
            return [self.model(**item) for item in response.data] if response.data else []
        except Exception as e:
            raise DatabaseException(str(e))
