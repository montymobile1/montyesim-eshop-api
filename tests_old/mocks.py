from app.models.user import UserModel
from app.schemas.app import DeviceRequest
from app.schemas.home import BundleDTO, CountryDTO, RegionDTO, BundleCategoryDTO


def get_bundle_mock():
    return BundleDTO(
        display_title="Test Bundle",
        display_subtitle="Test Subtitle",
        bundle_code="B123",
        bundle_category=BundleCategoryDTO(code="GLOBAL", title="GLOBAL", type="GLOBAL"),
        bundle_marketing_name="Test Marketing Name",
        bundle_name="Test Bundle Name",
        count_countries=10,
        currency_code="USD",
        gprs_limit_display="5GB",
        price=10.99,
        price_display="$10.99",
        unlimited=False,
        validity=30,
        plan_type="Data only",
        activity_policy="The validity period starts when the eSIM connects to any supported networks.",
        validity_display="30 days",
        countries=get_country_mocks(),
        icon="https://placehold.co/400x400",
        label=None,
        gprs_limit=5
    )


def get_bundle_mocks():
    return [get_bundle_mock() for _ in range(10)]


def get_region_mock():
    return RegionDTO(region_code="ASIA", zone_name="Asia", region_name="Asia", icon="https://placehold.co/400x400",
                     guid="12312312")


def get_region_mocks():
    return [get_region_mock() for _ in range(3)]


def get_country_mock():
    return CountryDTO(country="Lebanon", country_code="LB", iso3_code="LB", zone_name="", id="leb123",
                      alternative_country="lebanon")


def get_country_mocks():
    return [get_country_mock() for _ in range(60)]


def get_user_model_mock():
    return UserModel(id="123", email="email", token="token", msisdn="123", is_verified=True, anonymous_user_id="",
                     is_anonymous=False, language="en")


def get_device_request_mock():
    return DeviceRequest.model_validate_json('''{
  "fcm_token": "string",
  "manufacturer": "string",
  "device_model": "string",
  "os": "string",
  "os_version": "string",
  "app_version": "string",
  "ram_size": "string",
  "screen_resolution": "string",
  "is_rooted": true
}''')
