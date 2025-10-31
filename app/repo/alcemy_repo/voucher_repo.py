from app.models.declare_models.voucher import Voucher  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class VoucherRepo(BaseRepository):
    def __init__(self):
        super().__init__(Voucher)
