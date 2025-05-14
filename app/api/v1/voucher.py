import os
from typing import Annotated

from app.dependencies.security import bearer_token, device_token
from app.models.user import UserModel
from app.schemas.response import Response
from app.schemas.voucher import VoucherRequestRedeem
from app.services.voucher_service import VoucherService
from fastapi import APIRouter, Depends, Header

router = APIRouter()

service = VoucherService()

@router.post("/redeem", response_model=Response,
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def assign(voucher_redeem_request: VoucherRequestRedeem, user: Annotated[UserModel, Depends(bearer_token)],
                 x_device_id: str = Header(None),x_currency: str = Header(os.getenv("DEFAULT_CURRENCY"))):
    return await service.redeem(voucher_redeem_request= voucher_redeem_request,user= user)
