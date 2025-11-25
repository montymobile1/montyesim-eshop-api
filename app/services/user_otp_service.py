from app.config.constants import ErrorMessages
from app.exceptions import CustomException
from app.models.user_otp import UserOtpModel
from app.repo.user_otp_repo import UserOtpRepo


class UserOtpService:
    def __init__(self):
        self.__user_otp_repo = UserOtpRepo()

    async def generate_otp(self, mobile: str, email: str = None):
        """
        Generate a 6-digit OTP for the given mobile number and store it in the database with an expiration time of 5 minutes.
        """
        import random
        from datetime import datetime, timedelta, timezone as dt_timezone

        # Limit: max 3 OTPs/hour per mobile
        if await self.__recent_otp_limit(mobile=mobile):
            raise CustomException(code=429, name=ErrorMessages.OTP_LIMIT_REACHED,
                                  details="Maximum OTP requests per hour reached. Please try again later.")
        if await self.__has_active_otp(mobile=mobile):
            raise CustomException(code=400, name=ErrorMessages.OTP_STILL_ACTIVE,
                                  details="An active OTP already exists. Please use the existing OTP or wait for it to expire.")

        otp = f"{random.randint(100000, 999999)}"
        # Check if OTP already exists for this mobile
        existing_otps = await self.__user_otp_repo.list(where={"mobile": mobile, "is_used": False, "otp": otp})
        if existing_otps or len(existing_otps) > 0:
            return await self.generate_otp(mobile)

        expire_at = (datetime.now(tz=dt_timezone.utc) + timedelta(minutes=5)).isoformat()
        await self.__user_otp_repo.create({
            "mobile": mobile,
            "email": email,
            "otp": otp,
            "expire_at": expire_at,
            "is_used": False
        })
        return otp

    async def verify_otp(self, otp: str, mobile: str, email: str = None):
        if not otp or len(otp) != 6 or not otp.isdigit():
            raise CustomException(code=400, details="OTP Invalid", name=ErrorMessages.OTP_INVALID)
        user_otp = await self.__get_otp(otp=otp, mobile=mobile, email=email)
        if not user_otp:
            raise CustomException(code=400, details="OTP Invalid", name=ErrorMessages.OTP_INVALID)
        if await self.__is_expired(otp=otp, mobile=mobile):
            raise CustomException(code=400, details="OTP Expired", name=ErrorMessages.OTP_EXPIRED)
        await self.use_otp(otp=otp, mobile=mobile)
        return True

    async def use_otp(self, otp: str, mobile: str):
        """ Mark the given OTP for the mobile number as used."""
        await self.__user_otp_repo.update_by({"mobile": mobile, "otp": otp}, {"is_used": True})

    async def __is_expired(self, otp: str, mobile: str):
        """
        Check if the given OTP for the mobile number is expired.
        """
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).isoformat()
        results = await self.__user_otp_repo.is_otp_expired(mobile=mobile, otp=otp, time=now, is_used=False)
        return len(results) > 0

    async def __has_active_otp(self, mobile: str) -> bool:
        """
        Check if there is any active (not used and not expired) OTP for the given mobile number.
        """
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).isoformat()
        results = await self.__user_otp_repo.has_active_otp(mobile=mobile, time=now, is_used=False)
        return len(results.data) > 0

    async def __recent_otp_limit(self, mobile: str) -> bool:
        """
        Check if the number of OTPs sent to the mobile in the last hour exceeds the allowed limit.
        Returns True if limit is reached/exceeded, False otherwise.
        """
        from datetime import datetime, timezone, timedelta
        time_range = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        max_allowed = 3
        results = await self.__user_otp_repo.recent_otp_limit_count(mobile=mobile, time_range=time_range)
        otp_count = len(results)
        return otp_count >= max_allowed

    async def __get_otp(self, otp: str, mobile: str, email: str) -> UserOtpModel | None:
        """
        Retrieve the OTP record for the given mobile number and OTP.
        """
        results = await self.__user_otp_repo.get_first_by(where={"mobile": mobile, "otp": otp, "email": email})
        if len(results) == 0:
            return None
        return UserOtpModel(**results.data[0])
