from app.config.db import DatabaseTables
from app.models.user import UserWalletModel, UserWalletTransactionModel
from app.repo.base_repo import BaseRepository


class UserWalletRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET, UserWalletModel)


class UserWalletTransactionRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_WALLET_TRANSACTION, UserWalletTransactionModel)
