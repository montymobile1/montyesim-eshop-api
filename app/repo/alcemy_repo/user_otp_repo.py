from app.models.declare_models.user_otp import UserOtp  # SQLAlchemy ORM model
from app.repo.alcemy_repo.base_repo import BaseRepository


class UserOtpRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserOtp)
