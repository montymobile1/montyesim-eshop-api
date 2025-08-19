import os
from typing import Annotated, List

from fastapi import APIRouter, Header, Depends

from app.dependencies.security import bearer_token, device_token
from app.models.user import UserModel
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionValidationRequest, ReferralRewardRequest, PromotionHistoryDto
from app.schemas.response import Response
from app.services.promotion_service import PromotionService

router = APIRouter()
promotion_service = PromotionService()


@router.post("/validation", response_model=Response[BundleDTO],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def check_promotion_validation(user: Annotated[UserModel, Depends(bearer_token)],
                                     promotion_validation_request: PromotionValidationRequest,
                                     x_currency: str = Header(os.getenv("DEFAULT_CURRENCY")),
                                     x_device_id: str = Header(None)) -> Response[BundleDTO]:
    return await promotion_service.validate_promotion_code(promotion_validation_request, x_currency, user.id,
                                                           device_id=x_device_id)


@router.post("/referral_code", response_model=Response, dependencies=[Depends(bearer_token), Depends(device_token)])
async def check_promotion_validation(user: Annotated[UserModel, Depends(bearer_token)],
                                     referral_reward_request: ReferralRewardRequest,
                                     x_currency: str = Header("x-currency"),
                                     x_device_id: str = Header(None)
                                     ) -> Response:
    return await promotion_service.referral_code_rewards(referral_reward_request=referral_reward_request,
                                                         user_id=user.id, device_id=x_device_id)


@router.get("/history", response_model=Response, dependencies=[Depends(bearer_token), Depends(device_token)])
async def check_promotion_validation(user: Annotated[UserModel, Depends(bearer_token)],
                                     x_currency: str = Header("x-currency"),
                                     ) -> Response[List[PromotionHistoryDto]]:
    return await promotion_service.history(user_id=user.id)


@router.post("/test-referral", response_model=Response, dependencies=[Depends(bearer_token), Depends(device_token)])
async def check_promotion_validation(user: Annotated[UserModel, Depends(bearer_token)],
                                     x_currency: str = Header("x-currency"),
                                     ) -> Response[List[PromotionHistoryDto]]:
    return await promotion_service.apply_referral_rewards_after_buy_bundle(user_id=user.id)
