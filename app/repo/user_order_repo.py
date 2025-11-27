from datetime import datetime
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

    @staticmethod
    def _convert_to_naive_datetime(time_value: str | datetime) -> datetime:
        """
        Helper method to convert string or timezone-aware datetime to timezone-naive datetime.
        This ensures compatibility with DateTime(timezone=False) database columns.

        Args:
            time_value: Either an ISO format string or datetime object

        Returns:
            Timezone-naive datetime object
        """
        if isinstance(time_value, str):
            # Handle ISO format strings with 'Z' suffix
            time_value = datetime.fromisoformat(time_value.replace('Z', '+00:00'))

        # Remove timezone info if present
        if time_value.tzinfo is not None:
            time_value = time_value.replace(tzinfo=None)

        return time_value

    async def is_otp_expired(self, order_id: str, time: str | datetime):
        async with self.get_session() as session:
            try:
                time = self._convert_to_naive_datetime(time)

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
