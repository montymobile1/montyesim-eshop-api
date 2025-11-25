from app.models.user_wallet import UserWalletModel
from app.models.user_wallet_transaction import UserWalletTransactionModel
from app.repo.base_repo import BaseRepository


class UserWalletRepo(BaseRepository):

    def __init__(self):
        super().__init__(UserWalletModel)


class UserWalletTransactionRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserWalletTransactionModel)
