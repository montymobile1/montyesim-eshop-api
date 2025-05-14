from app.config.db import DatabaseTables
from app.models.user import UserOrderModel, UserProfileModel, UserProfileBundleModel, UsersCopyModel
from app.repo.base_repo import BaseRepository


class UserOrderRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_ORDER, UserOrderModel)


class UserProfileRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_PROFILE, UserProfileModel)


class UserProfileBundleRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_PROFILE_BUNDLE, UserProfileBundleModel)


class UserRepo(BaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_USER_COPY, UsersCopyModel)
