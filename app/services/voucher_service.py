from loguru import logger

from app.config.constants import UserWalletTransactionSource, ErrorMessages
from app.exceptions import CustomException
from app.models.user import UserModel
from app.models.voucher import VoucherModel
from app.repo.voucher_repo import VoucherRepo
from app.schemas.response import ResponseHelper
from app.schemas.voucher import VoucherRequestRedeem
from app.services.user_wallet_service import UserWalletService


class VoucherService:

    def __init__(self):
        self.__voucher_repo = VoucherRepo()
        self.__user_wallet_service = UserWalletService()

    def redeem(self, voucher_redeem_request: VoucherRequestRedeem, user: UserModel, x_currency: str):
        is_used = self.__voucher_repo.get_first_by(where={"is_used": True, "code": voucher_redeem_request.code})
        if is_used:
            raise CustomException(code=400, name=ErrorMessages.VOUCHER_ALREADY_USED,
                                  details="Voucher Already Used")
        voucher: VoucherModel = self.__voucher_repo.get_first_by(
            where={"code": voucher_redeem_request.code, "is_active": True, "is_used": False})
        if not voucher:
            raise CustomException(code=404, name=ErrorMessages.INVALID_VOUCHER_CODE,
                                  details="Invalid Voucher Code")
        # Check if voucher is expired using only the date part (ignore time)
        from datetime import datetime, timezone
        if voucher.expired_at:
            expired_at_dt = datetime.fromisoformat(voucher.expired_at)
            if expired_at_dt.tzinfo is None:
                expired_at_dt = expired_at_dt.replace(tzinfo=timezone.utc)
            # Compare only the date part
            if expired_at_dt.date() < datetime.now(timezone.utc).date():
                raise CustomException(code=400, name=ErrorMessages.VOUCHER_EXPIRED,
                                      details="Voucher Expired")

        try:
            self.__user_wallet_service.add_wallet_transaction(amount=voucher.amount,
                                                              user_id=user.id,
                                                              source=UserWalletTransactionSource.VOUCHER,
                                                              order_currency="USD")
            self.__voucher_repo.update_by(where={"id": voucher.id}, data={"used_by": user.id, "is_used": True})
            return ResponseHelper.success_response()
        except Exception as ex:
            logger.error(str(ex))
            raise CustomException(code=400, name=ErrorMessages.TOPUP_FAILED,
                                  details="TopUp Failed")
