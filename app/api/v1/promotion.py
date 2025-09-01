from typing import List

from fastapi import APIRouter, Depends

from app.dependencies.currency import x_currency_header
from app.dependencies.language import accept_language_header
from app.dependencies.security import bearer_token, device_token
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionValidationRequest, PromotionHistoryDto
from app.schemas.response import Response
from app.services.promotion_service import PromotionService

router = APIRouter()
promotion_service = PromotionService()


@router.post("/validation", response_model=Response[BundleDTO],
             dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                           Depends(x_currency_header)])
async def check_promotion_validation(promotion_validation_request: PromotionValidationRequest) -> Response[BundleDTO]:
    return await promotion_service.validate_promotion_code(promotion_validation_request)


@router.get("/history", response_model=Response,
            dependencies=[Depends(bearer_token), Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
async def check_promotion_validation() -> Response[List[PromotionHistoryDto]]:
    return await promotion_service.history()


@router.get("/referral-info", response_model=Response,
            dependencies=[Depends(device_token), Depends(accept_language_header),
                          Depends(x_currency_header)])
def get_referral_info() -> Response:
    return promotion_service.referral_info()
