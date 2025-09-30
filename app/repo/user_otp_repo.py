from app.config.db import DatabaseTables
from app.models.app import UserOtpModel
from app.repo.base_repo import BaseRepository


class UserOtpRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_OTP, UserOtpModel)
