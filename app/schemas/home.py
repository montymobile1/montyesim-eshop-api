import os
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, field_validator


class CountryDTO(BaseModel):
    id: str
    alternative_country: Optional[str]
    country: str
    country_code: Optional[str]
    iso3_code: Optional[str]
    zone_name: Optional[str]
    icon: Optional[str] = "https://placehold.co/400x400"
    operator_list: Optional[List[str]] = None


class BundleCategoryDTO(BaseModel):
    type: str
    title: str
    code: str


class RegionDTO(BaseModel):
    region_code: str
    region_name: str
    zone_name: str
    icon: str
    guid: str


class BundleDTO(BaseModel):
    display_title: str
    display_subtitle: str
    bundle_code: str
    bundle_category: BundleCategoryDTO | None
    bundle_region: List[RegionDTO] = []
    bundle_marketing_name: str
    bundle_name: str
    count_countries: int
    currency_code: str
    gprs_limit: Optional[float] = 0
    gprs_limit_display: str
    original_price: Optional[float] = 0
    price: float
    price_display: str
    unlimited: bool
    validity: int
    validity_label: Optional[str] =None
    plan_type: str = "Data only"
    activity_policy: str = "The validity period starts when the eSIM connects to any supported networks."
    validity_display: str
    countries: List[CountryDTO]
    icon: Optional[str] = "https://placehold.co/400x400"
    label: Optional[str] = None
    is_stockable: Optional[bool] = True
    bundle_info_code: Optional[str] = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    @field_validator("icon", mode="after")
    @classmethod
    def update_icon(cls, value, values):
        SUPABASE_URL = os.getenv("SUPABASE_URL")
        bundle_data = values.data
        if bundle_data['bundle_category'].type == "GLOBAL":
            return f"{SUPABASE_URL}/storage/v1/object/public/media/region/global.png"
        elif len(bundle_data['bundle_region']) > 0:
            return bundle_data['bundle_region'][0].icon
        elif len(bundle_data['countries']) > 0:
            return bundle_data['countries'][0].icon
        else:
            return f"{SUPABASE_URL}/storage/v1/object/public/media/region/global.png"


class HomeResponseDto(BaseModel):
    countries: List[CountryDTO]
    cruise_bundles: List[BundleDTO]
    global_bundles: List[BundleDTO]
    regions: List[RegionDTO]


class AllBundleResponse(BaseModel):
    total_rows: int
    bundles: List[BundleDTO]

class CurrencyDto(BaseModel):
    currency: str