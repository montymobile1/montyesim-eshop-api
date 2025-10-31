from app.models.declare_models.user_order import UserOrder
from app.models.declare_models.user_profile import UserProfile
from app.models.declare_models.user_profile_bundle import UserProfileBundle
from app.models.declare_models.users_copy import UsersCopy
from app.repo.alcemy_repo.base_repo import BaseRepository


class UserOrderRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserOrder)


class UserProfileRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserProfile)


class UserProfileBundleRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserProfileBundle)


class UserRepo(BaseRepository):
    def __init__(self):
        super().__init__(UsersCopy)

    def referral_code_key(self):
        return "metadata ->> referral_code"
