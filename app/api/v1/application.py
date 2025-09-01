from typing import List

from fastapi import APIRouter, Depends, Header, Request

from app.dependencies.language import accept_language_header
from app.dependencies.security import bearer_token, device_token, get_user_from_token
from app.models.user import UserModel
from app.schemas.app import DeviceRequest, DeleteDeviceRequest, FaqResponse, GlobalConfiguration, BannerResponse
from app.schemas.home import CurrencyDto
from app.schemas.response import Response
from app.services.app_service import AppService
from app.services.currency_service import CurrencyService

service = AppService()
currency_service = CurrencyService()
router = APIRouter()


@router.post("/device", response_model=Response, dependencies=[Depends(device_token), Depends(accept_language_header)])
async def add_device(device_request: DeviceRequest, request: Request, authorization: str = Header(None)) -> Response:
    user: UserModel = get_user_from_token(authorization)
    return await service.add_device(user=user, device_request=device_request, request=request)


@router.delete("/device", response_model=Response,
               dependencies=[Depends(bearer_token), Depends(accept_language_header)])
async def delete_device(delete_request: DeleteDeviceRequest) -> Response:
    return await service.delete_device(delete_device_request=delete_request)


@router.get("/faq", response_model=Response[List[FaqResponse]],
            dependencies=[Depends(device_token), Depends(accept_language_header)])
async def get_faq() -> Response:
    return await service.faq()


@router.get("/user-guide", response_model=Response,
            dependencies=[Depends(device_token), Depends(accept_language_header)])
async def user_guide():
    return await service.user_guide()


@router.get("/configurations", response_model=Response[List[GlobalConfiguration]],
            dependencies=[Depends(device_token), Depends(accept_language_header)])
async def configurations():
    return await service.configurations()


@router.get("/banner", response_model=Response[List[BannerResponse]],
            dependencies=[Depends(device_token), Depends(accept_language_header)])
async def get_banners() -> Response:
    return service.banners()


@router.get("/currency", response_model=Response[List[CurrencyDto]],
            dependencies=[Depends(device_token), Depends(accept_language_header)])
async def get_currencies() -> Response:
    return currency_service.get_all_currency()
