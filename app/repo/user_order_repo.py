from app.models.user_order import UserOrderModel
from app.models.user_profile import UserProfileModel
from app.models.user_profile_bundle import UserProfileBundleModel
from app.models.user import UsersCopyModel
from app.repo.base_repo import BaseRepository


class UserOrderRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserOrderModel)


class UserProfileRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserProfileModel)


class UserProfileBundleRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserProfileBundleModel)


class UserRepo(BaseRepository):
    def __init__(self):
        super().__init__(UsersCopyModel)

    def referral_code_key(self):
        return "metadata ->> referral_code"
