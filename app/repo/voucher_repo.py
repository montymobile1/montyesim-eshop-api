from app.config.db import DatabaseTables
from app.models.voucher import VoucherModel
from app.repo.base_repo import BaseRepository


class VoucherRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_VOUCHER, VoucherModel)


