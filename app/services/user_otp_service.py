from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.config.db import DatabaseTables
from app.exceptions import CustomException
from app.models.app import UserOtpModel
from app.repo.user_otp_repo import UserOtpRepo


class UserOtpService:
    def __init__(self):
        self.__user_otp_repo = UserOtpRepo()
        self.__supabase_client = supabase_client()

    def generate_otp(self, mobile: str, email: str = None):
        """
        Generate a 6-digit OTP for the given mobile number and store it in the database with an expiration time of 5 minutes.
        """
        import random
        from datetime import datetime, timedelta, timezone as dt_timezone

        # Limit: max 3 OTPs/hour per mobile
        if self.__recent_otp_limit(mobile=mobile):
            raise CustomException(code=429, name=ErrorMessages.OTP_LIMIT_REACHED,
                                  details="Maximum OTP requests per hour reached. Please try again later.")
        if self.__has_active_otp(mobile=mobile):
            raise CustomException(code=400, name=ErrorMessages.OTP_STILL_ACTIVE,
                                  details="An active OTP already exists. Please use the existing OTP or wait for it to expire.")

        otp = f"{random.randint(100000, 999999)}"
        # Check if OTP already exists for this mobile
        existing_otps = self.__user_otp_repo.list(where={"mobile": mobile, "is_used": False, "otp": otp})
        if existing_otps or len(existing_otps) > 0:
            return self.generate_otp(mobile)

        expire_at = (datetime.now(tz=dt_timezone.utc) + timedelta(minutes=5)).isoformat()
        self.__user_otp_repo.create({
            "mobile": mobile,
            "email": email,
            "otp": otp,
            "expire_at": expire_at,
            "is_used": False
        })
        return otp

    def verify_otp(self, otp: str, mobile: str, email: str = None):
        if not otp or len(otp) != 6 or not otp.isdigit():
            raise CustomException(code=400, details="OTP Invalid", name=ErrorMessages.OTP_INVALID)
        user_otp = self.__get_otp(otp=otp, mobile=mobile, email=email)
        if not user_otp:
            raise CustomException(code=400, details="OTP Invalid", name=ErrorMessages.OTP_INVALID)
        if self.__is_expired(otp=otp, mobile=mobile):
            raise CustomException(code=400, details="OTP Expired", name=ErrorMessages.OTP_EXPIRED)
        self.use_otp(otp=otp, mobile=mobile)
        return True

    def use_otp(self, otp: str, mobile: str):
        """ Mark the given OTP for the mobile number as used."""
        self.__user_otp_repo.update_by({"mobile": mobile, "otp": otp}, {"is_used": True})

    def __is_expired(self, otp: str, mobile: str):
        """
        Check if the given OTP for the mobile number is expired.
        """
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).isoformat()
        results = (self.__supabase_client.table(DatabaseTables.TABLE_USER_OTP)
                   .select("*")
                   .eq("mobile", mobile)
                   .eq("otp", otp)
                   .eq("is_used", False)
                   .lt("expire_at", now)
                   .execute())
        return len(results.data) > 0

    def __has_active_otp(self, mobile: str) -> bool:
        """
        Check if there is any active (not used and not expired) OTP for the given mobile number.
        """
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).isoformat()
        results = (self.__supabase_client.table(DatabaseTables.TABLE_USER_OTP)
                   .select("*")
                   .eq("mobile", mobile)
                   .eq("is_used", False)
                   .gt("expire_at", now)
                   .execute())
        return len(results.data) > 0

    def __recent_otp_limit(self, mobile: str) -> bool:
        """
        Check if the number of OTPs sent to the mobile in the last hour exceeds the allowed limit.
        Returns True if limit is reached/exceeded, False otherwise.
        """
        from datetime import datetime, timezone, timedelta
        time_range = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        max_allowed = 3
        results = (self.__supabase_client.table(DatabaseTables.TABLE_USER_OTP)
                   .select("*")
                   .eq("mobile", mobile)
                   .gt("expire_at", time_range)
                   .execute())
        otp_count = len(results.data)
        return otp_count >= max_allowed

    def __get_otp(self, otp: str, mobile: str, email: str) -> UserOtpModel | None:
        """
        Retrieve the OTP record for the given mobile number and OTP.
        """
        results = (self.__supabase_client.table(DatabaseTables.TABLE_USER_OTP)
                   .select("*")
                   .eq("mobile", mobile)
                   .eq("email", email)
                   .eq("otp", otp)
                   .execute())
        if len(results.data) == 0:
            return None
        return UserOtpModel(**results.data[0])
