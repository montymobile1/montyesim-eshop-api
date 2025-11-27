from datetime import datetime

from sqlalchemy import text

from app.exceptions import DatabaseException
from app.models.user_otp import UserOtpModel
from app.repo.base_repo import BaseRepository


class UserOtpRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserOtpModel)

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

    async def recent_otp_limit_count(self, mobile: str, time_range: str | datetime):
        async with self.get_session() as session:
            try:
                time_range = self._convert_to_naive_datetime(time_range)

                stmt = text("""
                            select *
                            from user_otp
                            where mobile = :mobile
                              and expire_at > :time_range
                            """)
                params = {"mobile": mobile, "time_range": time_range}
                result = await session.execute(stmt, params)
                rows = result.fetchall()
                return rows
            except Exception as e:
                raise DatabaseException(str(e))

    async def has_active_otp(self, mobile: str, time: str | datetime, is_used: bool):
        async with self.get_session() as session:
            try:
                time = self._convert_to_naive_datetime(time)

                stmt = text("""
                            select *
                            from user_otp
                            where mobile = :mobile
                              and is_used = :is_used
                              and expire_at > :time
                            """)
                params = {"mobile": mobile, "time": time, "is_used": is_used}
                result = await session.execute(stmt, params)
                rows = result.fetchall()
                return rows
            except Exception as e:
                raise DatabaseException(str(e))

    async def is_otp_expired(self, mobile: str, otp: str, time: str | datetime, is_used: bool):
        async with self.get_session() as session:
            try:
                time = self._convert_to_naive_datetime(time)

                stmt = text("""
                            select *
                            from user_otp
                            where mobile = :mobile
                              and is_used = :is_used
                              and otp = :otp
                              and expire_at < :time
                            """)
                params = {"mobile": mobile, "time": time, "is_used": is_used, "otp": otp}
                result = await session.execute(stmt, params)
                rows = result.fetchall()
                return rows
            except Exception as e:
                raise DatabaseException(str(e))
