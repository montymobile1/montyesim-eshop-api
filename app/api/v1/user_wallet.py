from fastapi import APIRouter, Depends

from app.config.context import auth_user_context, currency_context
from app.dependencies.currency import x_currency_header
from app.dependencies.language import accept_language_header
from app.dependencies.security import bearer_token, device_token
from app.schemas.bundle import PaymentIntentResponse
from app.schemas.response import Response, ResponseHelper
from app.schemas.user_wallet import UserWalletResponse, TopUpWalletRequest
from app.services.user_wallet_service import UserWalletService

router = APIRouter()
service = UserWalletService()


@router.get("/user_wallet_by_id/{user_wallet_id}", response_model=Response[UserWalletResponse],
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def get_user_wallet_by_id(user_wallet_id: str) -> Response[UserWalletResponse]:
    wallet = await service.get_user_wallet_by_id(user_wallet_id=user_wallet_id)
    count = 1 if wallet else 0
    return ResponseHelper.success_data_response(wallet, count)


@router.get("/user_wallet_by_user", response_model=Response[UserWalletResponse],
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def get_user_wallet_by_user_id() -> Response[
    UserWalletResponse]:
    user = auth_user_context.get()
    wallet = await service.get_user_wallet_by_user_id(user_id=user.id, currency_code=currency_context.get())
    count = 1 if wallet else 0
    return ResponseHelper.success_data_response(wallet, count)


@router.post("/top-up", response_model=Response[PaymentIntentResponse],
             dependencies=[Depends(device_token), Depends(bearer_token)])
async def top_up_wallet(top_up_request: TopUpWalletRequest) -> \
        Response[PaymentIntentResponse]:
    return await service.top_up_wallet(top_up_request=top_up_request, user=auth_user_context.get())
