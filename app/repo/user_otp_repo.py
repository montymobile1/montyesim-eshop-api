from datetime import datetime

from sqlalchemy import text

from app.exceptions import DatabaseException
from app.models.user_otp import UserOtpModel
from app.repo.base_repo import BaseRepository


class UserOtpRepo(BaseRepository):
    def __init__(self):
        super().__init__(UserOtpModel)

    async def recent_otp_limit_count(self, mobile: str, time_range: str | datetime):
        async with self.get_session() as session:
            try:
                # Convert string to datetime if needed
                if isinstance(time_range, str):
                    time_range = datetime.fromisoformat(time_range.replace('Z', '+00:00'))

                # Remove timezone info to match database column (timezone=False)
                if time_range.tzinfo is not None:
                    time_range = time_range.replace(tzinfo=None)

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
                # Convert string to datetime if needed
                if isinstance(time, str):
                    time = datetime.fromisoformat(time.replace('Z', '+00:00'))

                # Remove timezone info to match database column (timezone=False)
                if time.tzinfo is not None:
                    time = time.replace(tzinfo=None)

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
                # Convert string to datetime if needed
                if isinstance(time, str):
                    time = datetime.fromisoformat(time.replace('Z', '+00:00'))

                # Remove timezone info to match database column (timezone=False)
                if time.tzinfo is not None:
                    time = time.replace(tzinfo=None)

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
