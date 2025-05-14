from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.security import bearer_token, device_token
from app.models.user import UserModel
from app.schemas.promotion import PromotionCodeDetailsResponse
from app.schemas.bundle import PaymentIntentResponse
from app.schemas.response import Response, ResponseHelper
from app.schemas.user_wallet import UserWalletResponse, TopUpWalletRequest
from app.services.user_wallet_service import UserWalletService

router = APIRouter()
service = UserWalletService()


@router.get("/user_wallet_by_id/{user_wallet_id}", response_model=Response[UserWalletResponse])
# , dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_user_wallet_by_id(user_wallet_id: str) -> Response[UserWalletResponse]:
    wallet = await service.get_user_wallet_by_id(user_wallet_id=user_wallet_id)
    count = 1 if wallet else 0
    return ResponseHelper.success_data_response(wallet, count)


@router.get("/user_wallet_by_user", response_model=Response[UserWalletResponse],
            dependencies=[Depends(bearer_token), Depends(device_token)])
async def get_user_wallet_by_user_id(user: Annotated[UserModel, Depends(bearer_token)]) -> Response[
    UserWalletResponse]:
    wallet = await service.get_user_wallet_by_user_id(user_id=user.id)
    count = 1 if wallet else 0
    return ResponseHelper.success_data_response(wallet, count)


@router.post("/top-up", response_model=Response[PaymentIntentResponse],
             dependencies=[Depends(device_token), Depends(bearer_token)])
async def top_up_wallet(top_up_request: TopUpWalletRequest, user: Annotated[UserModel, Depends(bearer_token)]) -> \
Response[PaymentIntentResponse]:
    return await service.top_up_wallet(top_up_request=top_up_request, user=user)
