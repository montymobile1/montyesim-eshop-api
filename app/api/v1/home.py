import os

from fastapi import APIRouter, Depends, Header

from app.dependencies.security import device_token
from app.schemas.home import HomeResponseDto
from app.schemas.response import Response
from app.services.home_service import HomeService

router = APIRouter()

service = HomeService()


@router.get("/", response_model=Response[HomeResponseDto], dependencies=[Depends(device_token)])
async def home(x_currency: str = Header(os.getenv("DEFAULT_CURRENCY")), accept_language: str = Header("en"), ) -> \
Response[HomeResponseDto]:
    return await service.home_v2(currency=x_currency, locale=accept_language)
