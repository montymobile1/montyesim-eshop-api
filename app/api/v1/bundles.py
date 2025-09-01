from typing import List

from fastapi import APIRouter, Depends, Query
from fastapi.params import Path

from app.dependencies.currency import x_currency_header
from app.dependencies.language import accept_language_header
from app.dependencies.security import device_token
from app.schemas.home import BundleDTO, RegionDTO, CountryDTO
from app.schemas.response import Response
from app.services.bundle_service import BundleService
from app.services.grouping_service import GroupingService

router = APIRouter()

service = BundleService()
grouping_service = GroupingService()


@router.get("/by-country", response_model=Response[List[BundleDTO]],
            dependencies=[Depends(device_token), Depends(accept_language_header), Depends(x_currency_header)])
async def bundles_by_country(
        country_codes: str = Query(None, title="Country Guids ",
                                   description="Country Guids to get bundles from")) -> Response:
    return await service.get_bundles_by_country(country_codes=country_codes)


@router.get("/by-region/{region_code}", response_model=Response[List[BundleDTO]],
            dependencies=[Depends(device_token), Depends(accept_language_header),Depends(x_currency_header)])
async def bundles_by_region(
        region_code: str = Path(description="region_code from the returned regions")) -> Response:
    return await service.get_bundles_by_region(region_code=region_code)


@router.get("/region", response_model=Response[List[RegionDTO]],
            dependencies=[Depends(device_token), Depends(accept_language_header),Depends(x_currency_header)])
async def list_all_regions() -> Response[List[RegionDTO]]:
    return await service.get_regions()


@router.get("/countries", response_model=Response[List[CountryDTO]],
            dependencies=[Depends(device_token), Depends(accept_language_header),Depends(x_currency_header)])
async def list_all_countries() -> Response[List[CountryDTO]]:
    return await service.get_countries()


@router.get("/translate_tag", dependencies=[Depends(device_token), Depends(accept_language_header),Depends(x_currency_header)])
async def translate():
    await grouping_service.translate_tags()


@router.get("/{bundle_code}", response_model=Response[BundleDTO],
            dependencies=[Depends(device_token), Depends(accept_language_header),Depends(x_currency_header)])
async def bundle_by_code(bundle_code: str) -> Response[BundleDTO]:
    return await service.get_bundle(bundle_code)
