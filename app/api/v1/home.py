from fastapi import APIRouter, Depends

from app.dependencies.currency import x_currency_header
from app.dependencies.language import accept_language_header
from app.dependencies.security import device_token
from app.schemas.home import HomeResponseDto
from app.schemas.response import Response
from app.services.home_service import HomeService

router = APIRouter()

service = HomeService()


@router.get("/", response_model=Response[HomeResponseDto],
            dependencies=[Depends(device_token), Depends(accept_language_header), Depends(x_currency_header)])
async def home() -> \
        Response[HomeResponseDto]:
    return await service.home_v2()


@router.get("/cruise", response_model=Response[HomeResponseDto],
            dependencies=[Depends(device_token), Depends(accept_language_header), Depends(x_currency_header)])
async def get_cruise_bundles() -> Response[HomeResponseDto]:
    return await service.get_cruise_bundles()


@router.get("/land", response_model=Response[HomeResponseDto],
            dependencies=[Depends(device_token), Depends(accept_language_header), Depends(x_currency_header)])
async def get_land_bundles() -> Response[HomeResponseDto]:
    return await service.get_land_bundles()
