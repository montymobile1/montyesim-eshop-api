from app.models.declare_models.user_wallet import UserWallet
from app.models.declare_models.user_wallet_transaction import UserWalletTransaction
from app.repo.alcemy_repo.base_repo import BaseRepository


class UserWalletRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserWallet)


class UserWalletTransactionRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserWalletTransaction)
