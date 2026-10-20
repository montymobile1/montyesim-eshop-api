import hashlib
import hmac
import secrets

from app.config import config
from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.config.db import DatabaseTables, ConfigKeysEnum
from app.config.helper import get_config
from app.exceptions import CustomException
from app.models.app import UserOtpModel
from app.repo.user_otp_repo import UserOtpRepo


def hash_otp(otp: str) -> str:
    """HMAC-SHA256 of the code, keyed with OTP_HMAC_SECRET, as 64 hex chars: the only form of a code stored."""
    return hmac.new(config.OTP_HMAC_SECRET.encode("utf-8"), otp.encode("utf-8"), hashlib.sha256).hexdigest()


class UserOtpService:
    def __init__(self):
        self.__user_otp_repo = UserOtpRepo()

    @staticmethod
    def _new_code() -> str:
        """6 digits from a CSPRNG, leading zeros kept."""
        return f"{secrets.randbelow(10 ** 6):06d}"

    def generate_otp(self, mobile: str, email: str = None):
        """
        Generate a 6-digit OTP for the given mobile number and store its hash in the database with an expiration time of 5 minutes.
        Returns the plain code, to be sent to the user; it is never stored.
        """
        from datetime import datetime, timedelta, timezone as dt_timezone

        # Limit: max 3 OTPs/hour per mobile
        if self.__recent_otp_limit(mobile=mobile):
            raise CustomException(code=400, name=ErrorMessages.OTP_LIMIT_REACHED,
                                  details="Maximum OTP requests per hour reached. Please try again later.")
        if self.__has_active_otp(mobile=mobile):
            raise CustomException(code=429, name=ErrorMessages.OTP_STILL_ACTIVE,
                                  details="An active OTP already exists. Please use the existing OTP or wait for it to expire.")

        otp = self._new_code()
        otp_hash = hash_otp(otp)
        # Check if OTP already exists for this mobile
        existing_otps = self.__user_otp_repo.list(where={"mobile": mobile, "is_used": False, "otp": otp_hash})
        if existing_otps or len(existing_otps) > 0:
            return self.generate_otp(mobile)
        expiration_time = int(get_config(ConfigKeysEnum.OTP_EXPIRATION_TIME))
        expire_at = (datetime.now(tz=dt_timezone.utc) + timedelta(minutes=expiration_time)).isoformat()
        self.__user_otp_repo.create({
            "mobile": mobile,
            "email": email,
            "otp": otp_hash,
            "expire_at": expire_at,
            "is_used": False
        })
        return otp

    def reissue_otp(self, mobile: str, email: str = None) -> str:
        """
        Resend: only the hash of a code is stored, so the previous code cannot be sent again. The active code
        of the mobile is expired (verify then refuses it as expired; it still counts towards the hourly limit)
        and a new one is generated. The hourly limit is checked first, so a refused resend leaves the current
        code working. The new code keeps the email of the code it replaces, which is what verify matches on
        (as when the same code used to be sent again).
        """
        from datetime import datetime, timezone
        if self.__recent_otp_limit(mobile=mobile):
            raise CustomException(code=400, name=ErrorMessages.OTP_LIMIT_REACHED,
                                  details="Maximum OTP requests per hour reached. Please try again later.")
        now = datetime.now(tz=timezone.utc).isoformat()
        replaced = (supabase_client().table(DatabaseTables.TABLE_USER_OTP)
                    .update({"expire_at": now})
                    .eq("mobile", mobile)
                    .eq("is_used", False)
                    .gt("expire_at", now)
                    .execute())
        previous_email = next((row.get("email") for row in replaced.data or [] if row.get("email")), None)
        return self.generate_otp(mobile=mobile, email=previous_email or email)

    def verify_otp(self, otp: str, mobile: str, email: str = None):
        if not otp or len(otp) != 6 or not otp.isdigit():
            raise CustomException(code=400, details="OTP Invalid", name=ErrorMessages.OTP_INVALID)
        otp_hash = hash_otp(otp)
        user_otp = self.__get_otp(otp_hash=otp_hash, mobile=mobile, email=email)
        if not user_otp:
            raise CustomException(code=400, details="OTP Invalid", name=ErrorMessages.OTP_INVALID)
        if self.__is_expired(otp_hash=otp_hash, mobile=mobile):
            raise CustomException(code=400, details="OTP Expired", name=ErrorMessages.OTP_EXPIRED)
        self.use_otp(otp_hash=otp_hash, mobile=mobile)
        return True

    def use_otp(self, otp_hash: str, mobile: str):
        """ Mark the OTP with the given hash for the mobile number as used."""
        self.__user_otp_repo.update_by({"mobile": mobile, "otp": otp_hash}, {"is_used": True})

    def __is_expired(self, otp_hash: str, mobile: str):
        """
        Check if the given OTP for the mobile number is expired.
        """
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).isoformat()
        results = (supabase_client().table(DatabaseTables.TABLE_USER_OTP)
                   .select("*")
                   .eq("mobile", mobile)
                   .eq("otp", otp_hash)
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
        results = (supabase_client().table(DatabaseTables.TABLE_USER_OTP)
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
        results = (supabase_client().table(DatabaseTables.TABLE_USER_OTP)
                   .select("*")
                   .eq("mobile", mobile)
                   .gt("expire_at", time_range)
                   .execute())
        otp_count = len(results.data)
        return otp_count >= max_allowed

    def __get_otp(self, otp_hash: str, mobile: str, email: str) -> UserOtpModel | None:
        """
        Retrieve the OTP record for the given mobile number whose stored hash matches, compared in constant time.
        A row stored before hashing (the plain 6-digit code) never matches a 64-char hash.
        """
        results = (supabase_client().table(DatabaseTables.TABLE_USER_OTP)
                   .select("*")
                   .eq("mobile", mobile)
                   .eq("email", email)
                   .execute())
        for row in results.data:
            if hmac.compare_digest(str(row.get("otp") or ""), otp_hash):
                return UserOtpModel(**row)
        return None
