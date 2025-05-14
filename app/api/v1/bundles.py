import os
from typing import List

from fastapi import APIRouter, Depends, Header, Query
from fastapi.params import Path

from app.dependencies.security import device_token
from app.schemas.home import BundleDTO, RegionDTO, CountryDTO
from app.schemas.response import Response
from app.services.bundle_service import BundleService

router = APIRouter()

service = BundleService()


@router.get("/by-country", response_model=Response[List[BundleDTO]],
            dependencies=[Depends(device_token)])
async def bundles_by_country(
        country_codes: str = Query(None, title="Country Guids ", description="Country Guids to get bundles from"),
        x_device_id: str = Header(None),
        accepted_language: str = Header("en"),
        x_currency: str = Header(os.getenv("DEFAULT_CURRENCY"))) -> Response:
    return await service.get_bundles_by_country(country_codes=country_codes,currency_name=x_currency)


@router.get("/by-region/{region_code}", response_model=Response[List[BundleDTO]],
            dependencies=[Depends(device_token)])
async def bundles_by_region(region_code: str = Path(description="region_code from the returned regions"),
                      x_device_id: str = Header(None),
                      accepted_language: str = Header("en"),
                      x_currency: str = Header(os.getenv("DEFAULT_CURRENCY"))) -> Response:
    return await service.get_bundles_by_region(region_code=region_code, currency=x_currency)


@router.get("/region", response_model=Response[List[RegionDTO]], dependencies=[Depends(device_token)])
async def list_all_regions(x_device_id: str = Header(None), accepted_language: str = Header("en")) -> Response[
    List[RegionDTO]]:
    return await service.get_regions()


@router.get("/countries", response_model=Response[List[CountryDTO]], dependencies=[Depends(device_token)])
async def list_all_countries(x_device_id: str = Header(None), accepted_language: str = Header("en")) -> Response[
    List[CountryDTO]]:
    return await service.get_countries()


@router.get("/{bundle_code}", response_model=Response[BundleDTO], dependencies=[Depends(device_token)])
async def bundle_by_code(bundle_code: str, x_device_id: str = Header(None), accepted_language: str = Header("en"),x_currency: str = Header(os.getenv("DEFAULT_CURRENCY"))) -> \
        Response[BundleDTO]:
    return await service.get_bundle(bundle_code,x_currency)
