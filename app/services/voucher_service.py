from loguru import logger

from app.exceptions import CustomException
from app.models.user import UserModel
from app.repo.voucher_repo import VoucherRepo
from app.schemas.response import ResponseHelper
from app.schemas.voucher import VoucherRequestRedeem
from app.services.currency_service import CurrencyService
from app.services.user_wallet_service import UserWalletService


class VoucherService:

    def __init__(self):
        self.__voucher_repo = VoucherRepo()
        self.__user_wallet_service = UserWalletService()
        self.__currency_service = CurrencyService()

    async def redeem(self, voucher_redeem_request: VoucherRequestRedeem, user: UserModel, x_currency: str):
        is_used = self.__voucher_repo.get_first_by(where={"is_used": True, "code": voucher_redeem_request.code})
        if is_used:
            raise CustomException(code=400, name="Voucher Already Used",
                                  details="Voucher Already Used")
        voucher = self.__voucher_repo.get_first_by(
            where={"code": voucher_redeem_request.code, "is_active": True, "is_used": False})
        if not voucher:
            raise CustomException(code=404, name="Invalid Voucher Code",
                                  details="Invalid Voucher Code")
        # Check if voucher is expired using only the date part (ignore time)
        from datetime import datetime, timezone
        if voucher.expired_at:
            expired_at_dt = datetime.fromisoformat(voucher.expired_at)
            if expired_at_dt.tzinfo is None:
                expired_at_dt = expired_at_dt.replace(tzinfo=timezone.utc)
            # Compare only the date part
            if expired_at_dt.date() <= datetime.now(timezone.utc).date():
                raise CustomException(code=400, name="Voucher Expired",
                                      details="Voucher Expired")

        try:
            rate = self.__currency_service.get_rate_by_currency(x_currency)
            await self.__user_wallet_service.add_wallet_transaction((voucher.amount * rate), user.id, "voucher")
            self.__voucher_repo.update_by(where={"id": voucher.id}, data={"used_by": user.id, "is_used": True})
            return ResponseHelper.success_response()
        except Exception as ex:
            logger.error(str(ex))
            raise CustomException(code=400, name="TopUp Failed",
                                  details="TopUp Failed")
