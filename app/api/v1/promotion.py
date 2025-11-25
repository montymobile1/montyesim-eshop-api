import os
from typing import Annotated, List

from fastapi import APIRouter, Header, Depends

from app.dependencies.security import bearer_token, device_token
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionValidationRequest, PromotionHistoryDto
from app.schemas.response import Response
from app.schemas.user import UserModel
from app.services.promotion_service import PromotionService

router = APIRouter()
promotion_service = PromotionService()


@router.post("/validation", response_model=Response[BundleDTO],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def check_promotion_validation(user: Annotated[UserModel, Depends(bearer_token)],
                                     promotion_validation_request: PromotionValidationRequest,
                                     x_currency: str = Header(os.getenv("DEFAULT_CURRENCY")),
                                     x_device_id: str = Header(None),
                                     accept_language: str = Header("en")) -> Response[BundleDTO]:
    return await promotion_service.validate_promotion_code(promotion_validation_request, x_currency, user.id,
                                                           device_id=x_device_id, locale=accept_language)


@router.get("/history", response_model=Response, dependencies=[Depends(bearer_token), Depends(device_token)])
async def check_promotion_validation(user: Annotated[UserModel, Depends(bearer_token)],
                                     x_currency: str = Header("x-currency"),
                                     ) -> Response[List[PromotionHistoryDto]]:
    return await promotion_service.history(user_id=user.id, x_currency=x_currency)


@router.get("/referral-info", response_model=Response, dependencies=[Depends(device_token)])
async def get_referral_info(accept_language: str = Header("en"),
                            x_currency: str = Header("x-currency")) -> Response:
    return await promotion_service.referral_info(x_currency=x_currency, locale=accept_language)
