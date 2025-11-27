from sqlalchemy import text

from app.exceptions import DatabaseException
from app.models.user import UsersCopyModel
from app.models.user_order import UserOrderModel
from app.models.user_profile import UserProfileModel
from app.models.user_profile_bundle import UserProfileBundleModel
from app.repo.base_repo import BaseRepository


class UserOrderRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserOrderModel)

    async def is_otp_expired(self, order_id: str, time: str):
        async with self.get_session() as session:
            try:
                stmt = text("""
                            select *
                            from user_order
                            where id = :order_id
                              and otp_expired_at < :time
                            """)
                params = {"order_id": order_id, "time": time}
                result = await session.execute(stmt, params)
                rows = result.fetchall()
                return rows
            except Exception as e:
                raise DatabaseException(str(e))


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
        return "metadata ->> 'referral_code'"
