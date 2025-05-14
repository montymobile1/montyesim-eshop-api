from typing import Annotated, List

from fastapi import APIRouter, Depends, Header, Request

from app.dependencies.security import bearer_token, device_token, get_user_from_token
from app.models.user import UserModel
from app.schemas.app import DeviceRequest, ContactUsRequest, DeleteDeviceRequest, FaqResponse, PageContentResponse, \
    GlobalConfiguration
from app.schemas.home import CurrencyDto
from app.schemas.response import Response
from app.services.app_service import AppService
from app.services.currency_service import CurrencyService

# service = AppMockService()
service = AppService()
currency_service = CurrencyService()
router = APIRouter()


@router.post("/device", response_model=Response, dependencies=[Depends(device_token)])
async def add_device(device_request: DeviceRequest, request: Request, authorization: str = Header(None),
                     accepted_language: str = Header("en"), x_device_id: str = Header(None)) -> Response:
    user: UserModel = get_user_from_token(authorization)
    return await service.add_device(user, x_device_id, device_request,request)


@router.delete("/device", response_model=Response, dependencies=[Depends(bearer_token), Depends(device_token)])
async def delete_device(delete_request: DeleteDeviceRequest, user: Annotated[UserModel, Depends(bearer_token)],
                        accepted_language: str = Header("en"), x_device_id: str = Header(None)) -> Response:
    return await service.delete_device(user, delete_request)


@router.get("/faq", response_model=Response[List[FaqResponse]],
            dependencies=[Depends(device_token)])
async def faq(accepted_language: str = Header("en"), x_device_id: str = Header(None)):
    return await service.faq(accepted_language)


@router.get("/about_us", response_model=Response[PageContentResponse], dependencies=[Depends(device_token)])
async def about_us(accepted_language: str = Header("en"), x_device_id: str = Header(None)):
    return await service.about_us(accepted_language)


@router.get("/privacy_policy", response_model=Response[PageContentResponse], dependencies=[Depends(device_token)])
async def about_us(accepted_language: str = Header("en"), x_device_id: str = Header(None)):
    return await service.privacy_policy(accepted_language)


@router.post("/contact", response_model=Response, dependencies=[Depends(device_token)])
async def contact(contact_us_request: ContactUsRequest, accepted_language: str = Header("en"),
                  x_device_id: str = Header(None)):
    return await service.contact_us(contact_us_request)


@router.get("/terms-and-conditions", response_model=Response[PageContentResponse], dependencies=[Depends(device_token)])
async def terms_and_conditions(accepted_language: str = Header("en"),
                               x_device_id: str = Header(None)):
    return await service.terms_and_conditions(accepted_language)


@router.get("/user-guide", response_model=Response, dependencies=[Depends(device_token)])
async def user_guide(accepted_language: str = Header("en"),
                     x_device_id: str = Header(None)):
    return await service.user_guide()


@router.get("/configurations", response_model=Response[List[GlobalConfiguration]], dependencies=[Depends(device_token)])
async def configurations(accepted_language: str = Header("en")):
    return await service.configurations()

@router.get("/currency", response_model=Response[List[CurrencyDto]], dependencies=[Depends(device_token)])
async def configurations(accepted_language: str = Header("en")):
    return currency_service.get_all_currency()
