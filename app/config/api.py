from enum import StrEnum


class EsimHubEndpoint(StrEnum):
    API_GET_REGIONS = "/configuration/api/v1/zone/get-all"
    API_GET_COUNTRIES = "/core/api/v1/catalog/Country/get-all"
    API_GET_BUNDLES_BY_CATEGORY = "/catalog/api/v1/Bundle/get-by-category-with-currency"
    API_GET_BUNDLES_BY_ZONE = "/catalog/api/v1/Bundle/get-by-zone-with-currency"
    API_GET_BUNDLE_BY_ID = "/catalog/api/v1/Bundle/get-by-id-with-currency"
    API_GET_BUNDLES_BY_COUNTRY = "/catalog/api/v1/BundleCountry/get-by-country-with-currency"
    API_SEARCH_BUNDLES_BY_COUNTRY = "/catalog/api/admin/v1/Bundle/search-by-countries"
    API_GET_TOPUP_RELATED_BUNDLES = "/core/api/v1/order/compatible-topup-with-currency"

    API_GET_ALL_BUNDLES = "/catalog/api/v1/Bundle/get-all-basic/active"

    API_GET_CONTENT_TAG = "/catalog/api/reseller/v1/Content/get-latest"
    API_GET_CONTENT_TAGS = "/catalog/api/reseller/v1/Content/get-all-content"
    API_GET_GLOBAL_CONFIGURATIONS = "/configuration/api/v1/globalconfiguration/get-by-keys"
    API_GET_BUNDLE_CONSUMPTION = "/core/api/v1/order/consumption"

    API_CREATE_RESELLER_TOPUP = "/core/api/v1/order/topup"
    API_CREATE_RESELLER_ORDER = "/core/api/v1/order/create"
    API_GET_ACTIVATION_CODE = "/core/api/v1/order/activation-code"

    API_CHECK_BUNDLE_APPLICABLE = "/core/api/v1/order/check-bundle-availability"

    API_EXCHANGE_RATE = "/api-gateway/billing/api/v1/exchangerate/get-all"


class DCBEndpoint(StrEnum):
    API_SEND_OTP = "/otp"
    API_DEDUCT = "/deduct"
