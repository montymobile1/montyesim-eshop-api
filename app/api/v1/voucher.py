from fastapi import APIRouter, Depends

from app.config.context import currency_context, auth_user_context
from app.dependencies.currency import x_currency_header
from app.dependencies.language import accept_language_header
from app.dependencies.security import bearer_token, device_token
from app.schemas.response import Response
from app.schemas.voucher import VoucherRequestRedeem
from app.services.voucher_service import VoucherService

router = APIRouter()

service = VoucherService()


@router.post("/redeem", response_model=Response,
             dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                           Depends(x_currency_header)])
async def assign(voucher_redeem_request: VoucherRequestRedeem):
    return await service.redeem(voucher_redeem_request=voucher_redeem_request, user=auth_user_context.get(),
                                x_currency=currency_context.get())
