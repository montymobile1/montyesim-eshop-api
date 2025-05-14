from app.exceptions import CustomException
from app.models.user import UserModel
from app.repo.voucher_repo import VoucherRepo
from app.schemas.response import ResponseHelper
from app.schemas.voucher import VoucherRequestRedeem
from app.services.user_wallet_service import UserWalletService
from loguru import logger


class VoucherService:

    def __init__(self):
        self.__voucher_repo = VoucherRepo()
        self.__user_wallet_service = UserWalletService()


    async def redeem(self,voucher_redeem_request : VoucherRequestRedeem,user: UserModel):
        voucher = self.__voucher_repo.get_first_by(where={"code" : voucher_redeem_request.code, "is_active" : True , "is_used" : False})
        if not voucher:
            raise CustomException(code=404, name="Voucher Redeem",
                                  details="Voucher Code Invalid")

        try:
            await self.__user_wallet_service.add_wallet_transaction(voucher.amount,user.id,"voucher")
            self.__voucher_repo.update_by(where={"id" : voucher.id},data={"used_by":user.id,"is_used" : True})
            return ResponseHelper.success_response()
        except Exception as ex:
            logger.error(str(ex))
            raise CustomException(code=400, name="Voucher Redeem",
                                  details="TopUp Failed")