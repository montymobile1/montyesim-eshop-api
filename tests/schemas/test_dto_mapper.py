"""
Comprehensive test suite for DtoMapper class
Tests all mapping functions with edge cases and error scenarios
"""
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock

import pytest
from gotrue import AuthResponse

from app.config.context import currency_context
from app.schemas.dto_mapper import DtoMapper
from app.models.notification import NotificationModel
from app.models.promotion import PromotionUsageModel
from app.models.user import (
    UserProfileModel,
    UserProfileBundleModel,
    CallBackNotificationInfoModel,
    UserOrderModel,
    UserWalletModel
)
from app.schemas.auth import AuthResponseDTO
from app.schemas.bundle import EsimBundleResponse, ConsumptionResponse, TransactionHistoryResponse
from app.schemas.home import BundleDTO, CountryDTO, RegionDTO, BundleCategoryDTO


class TestDtoMapperToBundleDto:
    """Test cases for to_bundle_dto method"""

    @pytest.fixture
    def valid_bundle_data(self):
        """Standard valid bundle data"""
        return {
            "recordGuid": "bundle-123",
            "price": 10.0,
            "exchangedPrice": 12.0,
            "isActive": True,
            "bundleInfo": {
                "gprsLimit": 5000,
                "dataUnit": "MB",
                "isStockable": True,
                "bundleCode": "CODE-123"
            },
            "validityPeriodCycle": {
                "details": [{"name": "30 Day"}]
            },
            "bundleDetails": [{
                "name": "Premium Data Plan",
                "description": "High-speed data"
            }],
            "bundleCategory": {
                "tag": "data",
                "name": "Data Bundle",
                "recordGuid": "cat-123"
            },
            "supportedZones": [],
            "supportedCountries": []
        }

    def test_to_bundle_dto_with_valid_data(self, valid_bundle_data):
        """Test successful mapping with complete valid data"""
        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data, currency="USD")

        assert isinstance(result, BundleDTO)
        assert result.bundle_code == "bundle-123"
        assert result.price == 12.0
        assert result.currency_code == "USD"
        assert result.gprs_limit == 5000
        assert result.gprs_limit_display == "5000 MB"
        assert result.validity == 30
        assert result.validity_label == "Day"
        assert result.unlimited is False
        assert result.is_active is True
        # Icon should be set to global.png since no regions or countries
        assert "global.png" in result.icon.lower()

    def test_to_bundle_dto_with_unlimited_data(self, valid_bundle_data):
        """Test mapping with unlimited data (negative gprsLimit)"""
        valid_bundle_data["bundleInfo"]["gprsLimit"] = -1

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data, currency="EUR")

        assert result.unlimited is True
        assert result.gprs_limit_display == "∞ Unlimited"
        assert result.gprs_limit == -1

    def test_to_bundle_dto_with_null_exchanged_price(self, valid_bundle_data):
        """Test when exchangedPrice is None, should use original price"""
        valid_bundle_data["exchangedPrice"] = None

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data, currency="GBP")

        assert result.price == 10.0
        assert result.original_price == 10.0

    def test_to_bundle_dto_with_empty_validity_details(self, valid_bundle_data):
        """Test when validity details are empty"""
        valid_bundle_data["validityPeriodCycle"]["details"] = []

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data)

        assert result.validity == 0
        assert result.validity_label == "Day"
        assert result.validity_display == "0 Day"

    def test_to_bundle_dto_with_rounded_price_display(self, valid_bundle_data):
        """Test price display with DISPLAY_PRICE=rounded environment variable"""
        with patch.dict(os.environ, {"DISPLAY_PRICE": "rounded"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data, currency="USD")
            # Price 12.0 should be displayed as integer
            assert result.price_display == "12 USD"

    def test_to_bundle_dto_with_normal_price_display(self, valid_bundle_data):
        """Test price display with DISPLAY_PRICE=normal (default)"""
        with patch.dict(os.environ, {"DISPLAY_PRICE": "normal"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data, currency="USD")
            assert result.price_display == "12.00 USD"



    def test_to_bundle_dto_with_countries_error(self, valid_bundle_data):
        """Test handling of country mapping errors"""
        valid_bundle_data["supportedCountries"] = [{"invalid": "data"}]

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data)

        assert result.countries == []
        assert result.count_countries == 0

    def test_to_bundle_dto_icon_url_generation(self, valid_bundle_data):
        """Test icon URL generation - should use global.png for bundles without regions/countries"""
        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(valid_bundle_data)
            # Icon should be global.png since no regions or countries
            assert "global.png" in result.icon.lower()

    def test_to_bundle_dto_icon_with_region(self):
        """Test icon uses region icon when bundle has regions"""
        bundle_data = {
            "recordGuid": "regional-bundle",
            "price": 10.0,
            "exchangedPrice": 12.0,
            "isActive": True,
            "bundleInfo": {"gprsLimit": 5000, "dataUnit": "MB", "isStockable": True, "bundleCode": "CODE-123"},
            "validityPeriodCycle": {"details": [{"name": "30 Day"}]},
            "bundleDetails": [{"name": "Regional Plan", "description": "Regional coverage"}],
            "bundleCategory": {"tag": "region", "name": "Regional", "recordGuid": "cat-123"},
            "supportedZones": [{
                "tag": "EU",
                "name": "Europe",
                "recordGuid": "region-eu"
            }],
            "supportedCountries": []
        }

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(bundle_data, currency="USD")
            # Icon should use the region icon
            assert "EU.png" in result.icon

    def test_to_bundle_dto_icon_with_country(self):
        """Test icon uses country icon when bundle has countries but no regions"""
        bundle_data = {
            "recordGuid": "country-bundle",
            "price": 10.0,
            "exchangedPrice": 12.0,
            "isActive": True,
            "bundleInfo": {"gprsLimit": 5000, "dataUnit": "MB", "isStockable": True, "bundleCode": "CODE-123"},
            "validityPeriodCycle": {"details": [{"name": "30 Day"}]},
            "bundleDetails": [{"name": "Country Plan", "description": "Single country"}],
            "bundleCategory": {"tag": "country", "name": "Country", "recordGuid": "cat-123"},
            "supportedZones": [],
            "supportedCountries": [{
                "recordGuid": "country-us",
                "name": "United States",
                "altName": "USA",
                "isoCode": "US",
                "isoCode3": "USA",
                "zone": "North America"
            }]
        }

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(bundle_data, currency="USD")
            # Icon should use the country icon
            assert "usa.png" in result.icon.lower()

    def test_to_bundle_dto_icon_with_global_category(self):
        """Test icon uses global.png for GLOBAL category"""
        bundle_data = {
            "recordGuid": "global-bundle",
            "price": 10.0,
            "exchangedPrice": 12.0,
            "isActive": True,
            "bundleInfo": {"gprsLimit": 5000, "dataUnit": "MB", "isStockable": True, "bundleCode": "CODE-123"},
            "validityPeriodCycle": {"details": [{"name": "30 Day"}]},
            "bundleDetails": [{"name": "Global Plan", "description": "Worldwide coverage"}],
            "bundleCategory": {"tag": "GLOBAL", "name": "Global", "recordGuid": "cat-123"},
            "supportedZones": [],
            "supportedCountries": []
        }

        with patch.dict(os.environ, {"SUPABASE_URL": "https://test.supabase.co"}):
            result = DtoMapper.to_bundle_dto(bundle_data, currency="USD")
            # Icon should be global.png
            assert "global.png" in result.icon.lower()


class TestDtoMapperToCountryDto:
    """Test cases for to_country_dto method"""

    def test_to_country_dto_with_complete_data(self):
        """Test country mapping with all fields"""
        country_data = {
            "recordGuid": "country-123",
            "name": "United States",
            "altName": "USA",
            "isoCode": "US",
            "isoCode3": "USA",
            "zone": "North America"
        }

        result = DtoMapper.to_country_dto(country_data)

        assert isinstance(result, CountryDTO)
        assert result.id == "country-123"
        assert result.country == "United States"
        assert result.alternative_country == "USA"
        assert result.country_code == "US"
        assert result.iso3_code == "USA"
        assert result.zone_name == "North America"
        assert "usa.png" in result.icon.lower()

    def test_to_country_dto_with_missing_optional_fields(self):
        """Test country mapping with missing optional fields"""
        country_data = {
            "recordGuid": "country-456"
        }

        result = DtoMapper.to_country_dto(country_data)

        assert result.country == "Unknown"
        assert result.alternative_country == ""
        assert result.country_code == "Unknown"
        assert result.iso3_code == "Unknown"
        assert result.zone_name == "Unknown"
        assert "generic.png" in result.icon


class TestDtoMapperToRegionDto:
    """Test cases for to_region_dto method"""

    def test_to_region_dto_with_complete_data(self):
        """Test region mapping with all fields"""
        region_data = {
            "tag": "EU",
            "name": "Europe",
            "recordGuid": "region-123"
        }

        result = DtoMapper.to_region_dto(region_data)

        assert isinstance(result, RegionDTO)
        assert result.region_code == "EU"
        assert result.region_name == "Europe"
        assert result.zone_name == "Europe"
        assert result.guid == "region-123"
        assert "EU.png" in result.icon

    def test_to_region_dto_with_missing_fields(self):
        """Test region mapping with missing fields"""
        region_data = {}

        result = DtoMapper.to_region_dto(region_data)

        assert result.region_code == "Unknown"
        assert result.region_name == "Unknown"
        assert result.zone_name == "Unknown"
        assert result.guid == "Unknown"


class TestDtoMapperToBundleCategoryDto:
    """Test cases for to_bundle_category_dto method"""

    def test_to_bundle_category_dto_with_complete_data(self):
        """Test bundle category mapping"""
        category_data = {
            "tag": "premium",
            "name": "Premium Plans",
            "recordGuid": "cat-123"
        }

        result = DtoMapper.to_bundle_category_dto(category_data)

        assert result.type == "premium"
        assert result.title == "Premium Plans"
        assert result.code == "cat-123"

    def test_to_bundle_category_dto_with_empty_data(self):
        """Test bundle category mapping with empty data"""
        category_data = {}

        result = DtoMapper.to_bundle_category_dto(category_data)

        assert result.type == "Unknown"
        assert result.title == "Unknown"
        assert result.code == "Unknown"


class TestDtoMapperToTransactionHistoryResponse:
    """Test cases for to_transaction_history_response method"""

    @pytest.fixture
    def user_profile_bundle(self):
        """Mock UserProfileBundleModel"""
        bundle = Mock(spec=UserProfileBundleModel)
        bundle.id="bundle-123"
        bundle.user_order_id = "order-123"
        bundle.iccid = "1234567890"
        bundle.bundle_type = Mock(value="Primary Bundle")
        bundle.plan_started = True
        bundle.bundle_expired = False
        #bundle.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bundle.created_at = "2025-01-01T00:00:00Z"
        bundle.bundle_data = {
            "bundle_code": "bundle-123",
            "price": 10.0,
            "currency_code": "USD",
            "display_title":"bundle-123-title",
            "display_subtitle":"bundle-123-subtitle",
            "bundle_category":None,
            "bundle_marketing_name":"bundle-123-marketing-name",
            "bundle_name":"bundle-123-name",
            "count_countries":1,
            "gprs_limit_display":"10",
            "price_display":"2 usd",
            "unlimited":False,
            "validity":"2",
            "validity_display":"2",
            "countries":[{
                "id":"1",
                "alternative_country":"LBN",
                "country":"Lebanon",
                "country_code":"LBN",
                "iso3_code":"LBN",
                "zone_name":"",
                "operator_list":[]
            }]






        }
        return bundle

    def test_to_transaction_history_response_with_bundle_data(self, user_profile_bundle):
        """Test transaction history mapping with bundle data"""
        result = DtoMapper.to_transaction_history_response(
            user_profile_bundle,
            x_currency="EUR",
            rate=0.9
        )

        assert isinstance(result, TransactionHistoryResponse)
        assert result.user_order_id == "order-123"
        assert result.iccid == "1234567890"
        assert result.bundle_type == "Primary Bundle"
        assert result.plan_started is True
        assert result.bundle_expired is False

    def test_to_transaction_history_response_without_bundle_data(self, user_profile_bundle):
        """Test transaction history when bundle_data is None"""
        user_profile_bundle.bundle_data = None

        result = DtoMapper.to_transaction_history_response(
            user_profile_bundle,
            x_currency="USD",
            rate=1.0
        )

        assert result.bundle is None


class TestDtoMapperGetProfileCurrentBundle:
    """Test cases for get_profile_current_bundle method"""

    def test_get_profile_current_bundle_with_no_bundles(self):
        """Test when user has no bundles"""
        profile = Mock(spec=UserProfileModel)
        profile.bundles = []

        result = DtoMapper.get_profile_current_bundle(profile)

        assert result is None

    def test_get_profile_current_bundle_with_single_bundle(self):
        """Test with single bundle"""
        profile = Mock(spec=UserProfileModel)
        bundle = Mock()
        bundle.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        profile.bundles = [bundle]

        result = DtoMapper.get_profile_current_bundle(profile)

        assert result == bundle

    def test_get_profile_current_bundle_prioritizes_active(self):
        """Test that active bundles are prioritized"""
        profile = Mock(spec=UserProfileModel)

        expired_bundle = Mock()
        expired_bundle.plan_started = True
        expired_bundle.bundle_expired = True
        expired_bundle.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)

        active_bundle = Mock()
        active_bundle.plan_started = True
        active_bundle.bundle_expired = False
        active_bundle.created_at = datetime(2024, 1, 2, tzinfo=timezone.utc)

        profile.bundles = [expired_bundle, active_bundle]

        result = DtoMapper.get_profile_current_bundle(profile)

        assert result == active_bundle

    def test_get_profile_current_bundle_with_string_created_at(self):
        """Test handling of string created_at dates"""
        profile = Mock(spec=UserProfileModel)

        bundle1 = Mock()
        bundle1.plan_started = False
        bundle1.bundle_expired = False
        bundle1.created_at = "2024-01-01T00:00:00Z"

        bundle2 = Mock()
        bundle2.plan_started = False
        bundle2.bundle_expired = False
        bundle2.created_at = "2024-01-02T00:00:00+00:00"

        profile.bundles = [bundle1, bundle2]

        result = DtoMapper.get_profile_current_bundle(profile)

        # Should return the most recent one (bundle2)
        assert result == bundle2

    def test_get_profile_current_bundle_with_invalid_dates(self):
        """Test handling of invalid date formats"""
        profile = Mock(spec=UserProfileModel)

        bundle = Mock()
        bundle.plan_started = False
        bundle.bundle_expired = False
        bundle.created_at = "invalid-date"
        bundle.bundle_data = None

        profile.bundles = [bundle]

        result = DtoMapper.get_profile_current_bundle(profile)

        # Should still return the bundle even with invalid date
        assert result == bundle


class TestDtoMapperMoveMatchingCountriesToTop:
    """Test cases for move_matching_countries_to_top method"""

    def test_move_matching_countries_to_top_with_matches(self):
        """Test moving matching countries to the top"""
        from app.schemas.bundle import CountryRequestDto

        country1 = Mock(spec=CountryDTO)
        country1.iso3_code = "USA"

        country2 = Mock(spec=CountryDTO)
        country2.iso3_code = "GBR"

        country3 = Mock(spec=CountryDTO)
        country3.iso3_code = "FRA"

        countries = [country1, country2, country3]

        search_country = Mock(spec=CountryRequestDto)
        search_country.iso3_code = "FRA"

        result = DtoMapper.move_matching_countries_to_top(countries, [search_country])

        # FRA should be first
        assert result[0].iso3_code == "FRA"
        assert len(result) == 3

    def test_move_matching_countries_to_top_with_no_matches(self):
        """Test when no countries match"""
        from app.schemas.bundle import CountryRequestDto

        country1 = Mock(spec=CountryDTO)
        country1.iso3_code = "USA"

        countries = [country1]

        search_country = Mock(spec=CountryRequestDto)
        search_country.iso3_code = "GBR"

        result = DtoMapper.move_matching_countries_to_top(countries, [search_country])

        # Order should remain the same
        assert result[0].iso3_code == "USA"

    def test_move_matching_countries_to_top_with_empty_search(self):
        """Test with empty search list"""
        country1 = Mock(spec=CountryDTO)
        country1.iso3_code = "USA"

        countries = [country1]

        result = DtoMapper.move_matching_countries_to_top(countries, [])

        assert result == countries


class TestDtoMapperToEsimBundleResponse:
    """Test cases for to_esim_bundle_response method"""

    @pytest.fixture
    def bundle_data(self):
        """Mock bundle data"""
        bundle_category = Mock(spec=BundleCategoryDTO)
        bundle_category.type="region"
        bundle_category.title = "Test Subtitle"
        bundle_category.code="A"


        bundle = Mock(spec=BundleDTO)
        bundle.display_title = "Test Bundle"
        bundle.display_subtitle = "Test Description"
        bundle.bundle_code = "bundle-123"
        bundle.bundle_category=bundle_category

        bundle.bundle_marketing_name = "Premium"
        bundle.bundle_name = "Premium Plan"
        bundle.count_countries = 5
        bundle.currency_code = "USD"
        bundle.gprs_limit_display = "5GB"
        bundle.unlimited = False
        bundle.validity = "30"
        bundle.validity_label = "Days"
        bundle.validity_display = "30 Days"
        bundle.plan_type = "data"
        bundle.countries = []
        bundle.original_price = 10.0
        bundle.plan_started = True
        bundle.bundle_expired = False
        bundle.label = None

        return bundle

    @pytest.fixture
    def user_profile_bundle(self):
        """Mock UserProfileBundleModel"""
        bundle = Mock(spec=UserProfileBundleModel)
        bundle.id = "bundle-123"
        bundle.user_order_id = "order-123"
        bundle.iccid = "1234567890"
        bundle.bundle_type = Mock(value="Primary Bundle")
        bundle.plan_started = True
        bundle.bundle_expired = False
        bundle.created_at = "2024-01-01T00:00:00Z"
        bundle.bundle_data = {
            "bundle_code": "bundle-123",
            "price": 10.0,
            "currency_code": "USD",
            "display_title": "bundle-123-title",
            "display_subtitle": "bundle-123-subtitle",
            "bundle_category": {
                "type": "a",
                "title": "bundle-123-title",
                "code": "A"},
            "bundle_marketing_name": "bundle-123-marketing-name",
            "bundle_name": "bundle-123-name",
            "count_countries": 1,
            "gprs_limit_display": "10",
            "price_display": "2 usd",
            "unlimited": False,
            "validity": "2",
            "plan_started": True,
            "validity_display": "2",
            "countries": [{
                "id": "1",
                "alternative_country": "LBN",
                "country": "Lebanon",
                "country_code": "LBN",
                "iso3_code": "LBN",
                "zone_name": "",
                "operator_list": []
            }]
        }
        return bundle

    @pytest.fixture
    def user_profile(self,user_profile_bundle):
        """Mock user profile"""
        profile = Mock(spec=UserProfileModel)
        profile.allow_topup = True
        profile.label = "My eSIM"
        profile.user_order_id = "order-123"
        profile.smdp_address = "smdp.example.com"
        profile.activation_code = "ABC123"
        profile.iccid = "1234567890"
        profile.validity = "2024-12-31"
        profile.created_at = "2024-01-01T00:00:00Z"
        profile.searched_countries = None
        profile.bundles = [user_profile_bundle]
        return profile



    def test_to_esim_bundle_response_inactive_order(self, user_profile, bundle_data):
        """Test eSIM bundle response with inactive order"""

        user_profile.plan_started = False
        user_profile.bundles[0].plan_started = False
        result = DtoMapper.to_esim_bundle_response(
            user_profile,
            bundle_data,
            rate=1.0,
            x_currency="USD",
            tax=0
        )

        assert isinstance(result, EsimBundleResponse)
        assert result.order_status == "Inactive"
        assert result.is_topup_allowed is True
        assert result.order_number == "order-123"
        assert result.iccid == "1234567890"

    def test_to_esim_bundle_response_with_active_bundle(self, user_profile, bundle_data):
        """Test with active bundle"""
        active_bundle = Mock()
        active_bundle.plan_started = True
        active_bundle.bundle_expired = False
        active_bundle.created_at ="2024-01-01T00:00:00Z"

        active_bundle.id = "bundle-123"
        active_bundle.user_order_id = "order-123"
        active_bundle.iccid = "1234567890"
        active_bundle.bundle_type = Mock(value="Primary Bundle")

        active_bundle.bundle_data = {
            "bundle_code": "bundle-123",
            "price": 10.0,
            "currency_code": "USD",
            "display_title": "bundle-123-title",
            "display_subtitle": "bundle-123-subtitle",
            "bundle_category": {
                "type": "a",
                "title": "bundle-123-title",
                "code": "A"},
            "bundle_marketing_name": "bundle-123-marketing-name",
            "bundle_name": "bundle-123-name",
            "count_countries": 1,
            "gprs_limit_display": "10",
            "price_display": "2 usd",
            "unlimited": False,
            "validity": "2",
            "plan_started": True,
            "validity_display": "2",
            "countries": [{
                "id": "1",
                "alternative_country": "LBN",
                "country": "Lebanon",
                "country_code": "LBN",
                "iso3_code": "LBN",
                "zone_name": "",
                "operator_list": []
            }]
        }

        user_profile.bundles = [active_bundle]

        result = DtoMapper.to_esim_bundle_response(
            user_profile,
            bundle_data,
            rate=1.0,
            x_currency="USD",
            tax=0
        )

        assert result.order_status == "Active"
        assert result.plan_started is True
        assert result.bundle_expired is False

    def test_to_esim_bundle_response_with_expired_bundle(self, user_profile, bundle_data):
        """Test with expired bundle"""
        expired_bundle = Mock()
        expired_bundle.plan_started = True
        expired_bundle.bundle_expired = True
        expired_bundle.created_at = "2024-01-01T00:00:00Z"

        expired_bundle.id = "bundle-123"
        expired_bundle.user_order_id = "order-123"
        expired_bundle.iccid = "1234567890"
        expired_bundle.bundle_type = Mock(value="Primary Bundle")

        expired_bundle.bundle_data = {
            "bundle_code": "bundle-123",
            "price": 10.0,
            "currency_code": "USD",
            "display_title": "bundle-123-title",
            "display_subtitle": "bundle-123-subtitle",
            "bundle_category": {
                "type": "a",
                "title": "bundle-123-title",
                "code": "A"},
            "bundle_marketing_name": "bundle-123-marketing-name",
            "bundle_name": "bundle-123-name",
            "count_countries": 1,
            "gprs_limit_display": "10",
            "price_display": "2 usd",
            "unlimited": False,
            "validity": "2",
            "plan_started": True,
            "plan_expired": True,
            "validity_display": "2",
            "countries": [{
                "id": "1",
                "alternative_country": "LBN",
                "country": "Lebanon",
                "country_code": "LBN",
                "iso3_code": "LBN",
                "zone_name": "",
                "operator_list": []
            }]
        }

        user_profile.bundles = [expired_bundle]

        result = DtoMapper.to_esim_bundle_response(
            user_profile,
            bundle_data,
            rate=1.0,
            x_currency="USD",
            tax=0
        )

        assert result.order_status == "Expired"

    def test_to_esim_bundle_response_with_tax(self, user_profile, bundle_data):
        """Test price calculation with tax"""
        result = DtoMapper.to_esim_bundle_response(
            user_profile,
            bundle_data,
            rate=1.0,
            x_currency="USD",
            tax=10.0  # 10% tax
        )

        # Price should be original_price * rate + (tax/100) * rate
        # 10.0 * 1.0 + (10.0/100) * 1.0 = 10.1
        assert result.price == 10.1

    def test_to_esim_bundle_response_with_currency_conversion(self, user_profile, bundle_data):
        """Test with currency conversion rate"""
        result = DtoMapper.to_esim_bundle_response(
            user_profile,
            bundle_data,
            rate=0.85,
            x_currency="EUR",
            tax=0
        )

        # Price should be 10.0 * 0.85 = 8.5
        assert result.price == 8.5
        assert "EUR" in result.price_display

    def test_to_esim_bundle_response_qr_code_generation(self, user_profile, bundle_data):
        """Test QR code value generation"""
        result = DtoMapper.to_esim_bundle_response(
            user_profile,
            bundle_data,
            rate=1.0,
            x_currency="USD",
            tax=0
        )

        expected_qr = f"LPA:1${user_profile.smdp_address}${user_profile.activation_code}"
        assert result.qr_code_value == expected_qr


class TestDtoMapperToUserNotificationResponse:
    """Test cases for to_user_notification_response method"""

    def test_to_user_notification_response_complete(self):
        """Test notification mapping with complete data"""
        notification = Mock(spec=NotificationModel)
        notification.id = 123
        notification.title = "Test Notification"
        notification.content = "Test Content"
        notification.created_at = "2024-01-01T00:00:00"
        notification.status = False
        notification.data = json.dumps({
            "transaction_status": "completed",
            "transaction": "TXN123",
            "transaction_message": "Success",
            "iccid": "1234567890",
            "category": "purchase",
            "translated_message": "Achevé"
        })

        result = DtoMapper.to_user_notification_response(notification)

        assert result.notification_id == 123
        assert result.title == "Test Notification"
        assert result.content == "Test Content"
        assert result.transaction_status == "completed"
        assert result.iccid == "1234567890"

    def test_to_user_notification_response_minimal_data(self):
        """Test with minimal data in JSON"""
        notification = Mock(spec=NotificationModel)
        notification.id = 4
        notification.title = "Minimal"
        notification.content = "Content"
        notification.created_at = "2024-01-01T00:00:00Z"
        notification.status = True
        notification.data = json.dumps({})

        result = DtoMapper.to_user_notification_response(notification)

        assert result.transaction_status == ""
        assert result.transaction == ""
        assert result.iccid == ""


class TestDtoMapperToConsumptionResponse:
    """Test cases for to_consumption_response method"""

    def test_to_consumption_response_complete(self):
        """Test consumption mapping"""
        data = {
            "dataAllocated": 5000,
            "dataUsed": 2500,
            "dataRemaining": 2500,
            "dataUnit": "MB",
            "planStatus": "active",
            "profileExpiryDate": "2024-12-31"
        }

        result = DtoMapper.to_consumption_response(data)

        assert isinstance(result, ConsumptionResponse)
        assert result.data_allocated == 5000
        assert result.data_used == 2500
        assert result.data_remaining == 2500
        assert result.data_allocated_display == "5000 MB"
        assert result.plan_status == "active"


class TestDtoMapperToOrderNotificationModel:
    """Test cases for to_order_notification_model method"""

    def test_to_order_notification_model_complete(self):
        """Test order notification mapping with complete data"""
        bundle = Mock(spec=UserProfileModel)
        bundle.label = "My Bundle"
        bundle.iccid = "1234567890"
        bundle.validity = "2024-12-31"
        bundle.smdp_address = "smdp.test.com"
        bundle.activation_code = "CODE123"
        bundle.allow_topup = True
        bundle.esim_hub_order_id = "hub-123"
        bundle.searched_countries = "USA,GBR"
        bundle.created_at = "2024-01-01T00:00:00Z"

        mock_bundle_dto = Mock(spec=BundleDTO)
        mock_bundle_dto.display_title = "Premium Plan"
        mock_bundle_dto.validity_display = "30 Days"

        bundle.bundles = [mock_bundle_dto]

        user_metadata = {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@example.com"
        }

        result = DtoMapper.to_order_notification_model(
            bundle,
            user_id="user-123",
            user_metadata=user_metadata,
            iccid="1234567890"
        )

        assert isinstance(result, CallBackNotificationInfoModel)
        assert result.user_id == "user-123"
        assert result.user_display_name == "John Doe"
        assert result.bundle_display_name == "My Bundle"
        assert result.iccid == "1234567890"

    def test_to_order_notification_model_fallback_name(self):
        """Test with missing user name, should use email"""
        bundle = Mock(spec=UserProfileModel)
        bundle.label = None
        bundle.iccid = "1234567890"
        bundle.validity = None
        bundle.smdp_address = "smdp.test.com"
        bundle.activation_code = "CODE123"
        bundle.allow_topup = False
        bundle.esim_hub_order_id = "hub-123"
        bundle.searched_countries = None
        bundle.created_at = datetime.now(timezone.utc)
        bundle.bundles = []

        user_metadata = {
            "email": "test@example.com"
        }

        result = DtoMapper.to_order_notification_model(
            bundle,
            user_id="user-456",
            user_metadata=user_metadata,
            iccid="1234567890"
        )

        assert result.user_display_name == "test@example.com"

    def test_to_order_notification_model_validity_calculation(self):
        """Test validity date calculation from validity_display"""
        bundle = Mock(spec=UserProfileModel)
        bundle.label = "Test"
        bundle.iccid = "1234567890"
        bundle.validity = None
        bundle.smdp_address = "smdp.test.com"
        bundle.activation_code = "CODE123"
        bundle.allow_topup = True
        bundle.esim_hub_order_id = "hub-123"
        bundle.searched_countries = None
        bundle.created_at = "2024-01-01T00:00:00Z"

        mock_bundle_dto = Mock(spec=BundleDTO)
        mock_bundle_dto.display_title = "Test Bundle"
        mock_bundle_dto.validity_display = "7 Days"

        bundle.bundles = [mock_bundle_dto]

        user_metadata = {"email": "test@example.com"}

        result = DtoMapper.to_order_notification_model(
            bundle,
            user_id="user-789",
            user_metadata=user_metadata,
            iccid="1234567890"
        )

        # Validity should be created_at + 7 days
        assert result.validity is not None
        assert isinstance(result.validity, str)


class TestDtoMapperToUserOrderHistory:
    """Test cases for to_user_order_history method"""

    def test_to_user_order_history_complete(self):
        """Test order history mapping"""
        user_order = Mock(spec=UserOrderModel)
        user_order.id = "order-123"
        user_order.payment_status = "completed"
        user_order.amount = 1000  # in cents
        user_order.modified_amount = None
        user_order.tax_amount = 100
        user_order.currency = "USD"
        user_order.created_at = "2024-01-01T00:00:00Z"
        user_order.order_type = "purchase"
        user_order.bundle_data = json.dumps({"bundle_code": "bundle-123", "price": 10.0})
        user_order.payment_type = "Card"

        user_order.bundle_data = json.dumps({
            "bundle_code": "bundle-456",
            "display_title": "bundle A",
            "display_title": "Test Bundle",
            "display_subtitle": "Test Description",
            "bundle_category": {
                "type": "premium",
                "title": "Premium Plans",
                "code": "cat-123"
            },
            "bundle_category.type": "data",
            "bundle_category.title": "Data Plans",
            "bundle_marketing_name": "Premium",
            "bundle_name": "Premium Plan",
            "count_countries": 5,
            "currency_code": "USD",
            "gprs_limit_display": "5GB",
            "unlimited": False,
            "validity": "30",
            "validity_label": "Days",
            "validity_display": "30 Days",
            "plan_type": "data",
            "countries": [],
            "original_price": 10.0,
            "price": 10.0,
            "price_display": "10"

        })

        with patch.dict(os.environ, {
            "MERCHANT_DISPLAY_NAME": "Test Company",
            "MERCHANT_ADDRESS": "123 Test St",
            "MERCHANT_PHONE": "+1234567890",
            "MERCHANT_EMAIL": "test@test.com",
            "MERCHANT_WEBSITE": "https://test.com"
        }):
            result = DtoMapper.to_user_order_history(user_order, rate=1.0, currency="USD")

        assert result.order_number == "order-123"
        assert result.order_status == "completed"
        assert result.order_currency == "USD"
        assert result.company_name == "Test Company"

    def test_to_user_order_history_with_modified_amount(self):
        """Test with modified amount (e.g., after discount)"""
        user_order = Mock(spec=UserOrderModel)
        user_order.id = "order-456"
        user_order.payment_status = "completed"
        user_order.amount = 1000
        user_order.modified_amount = 800  # discounted
        user_order.tax_amount = 80
        user_order.currency = "USD"
        user_order.created_at = "2024-01-01T00:00:00+00:00"
        user_order.order_type = "purchase"

        user_order.payment_type = "Wallet"
        user_order.bundle_data = json.dumps({
            "bundle_code": "bundle-456",
            "display_title": "bundle A",
            "display_title": "Test Bundle",
            "display_subtitle": "Test Description",
            "bundle_category": {
                "type": "premium",
                "title": "Premium Plans",
                "code": "cat-123"
            },
            "bundle_category.type": "data",
            "bundle_category.title": "Data Plans",
            "bundle_marketing_name": "Premium",
            "bundle_name": "Premium Plan",
            "count_countries": 5,
            "currency_code": "USD",
            "gprs_limit_display": "5GB",
            "unlimited": False,
            "validity": "30",
            "validity_label": "Days",
            "validity_display": "30 Days",
            "plan_type": "data",
            "countries": [],
            "original_price": 10.0,
            "price": 10.0,
            "price_display": "10"

        })


        result = DtoMapper.to_user_order_history(user_order, rate=1.0, currency="USD")

        # Should use modified_amount (800) + tax (80) = 880
        assert result.order_amount == 880

    def test_to_user_order_history_with_currency_conversion(self):
        """Test order history with currency conversion"""
        user_order = Mock(spec=UserOrderModel)
        user_order.id = "order-789"
        user_order.payment_status = "completed"
        user_order.amount = 1000
        user_order.modified_amount = None
        user_order.tax_amount = 100
        user_order.currency = "USD"
        user_order.created_at = "2024-01-01T00:00:00+00:00"
        user_order.order_type = "purchase"
        user_order.bundle_data = json.dumps({
            "bundle_code": "bundle-789",
            "display_title":"bundle A",
            "display_title":  "Test Bundle",
            "display_subtitle": "Test Description",
            "bundle_code": "bundle-123",
            "bundle_category": {
            "type": "premium",
            "title": "Premium Plans",
            "code": "cat-123"
        },
            "bundle_category.type": "data",
            "bundle_category.title": "Data Plans",
            "bundle_marketing_name": "Premium",
            "bundle_name": "Premium Plan",
            "count_countries": 5,
            "currency_code": "USD",
            "gprs_limit_display": "5GB",
            "unlimited": False,
            "validity": "30",
            "validity_label": "Days",
            "validity_display": "30 Days",
            "plan_type": "data",
            "countries": [],
            "original_price": 10.0,
            "price": 10.0,
            "price_display":"10"

        })
        user_order.payment_type = "Card"

        result = DtoMapper.to_user_order_history(user_order, rate=0.85, currency="EUR")

        # (1000 + 100) * 0.85 = 935
        assert result.order_amount == 935.0


class TestDtoMapperToPageContentResponse:
    """Test cases for to_page_content_response method"""

    def test_to_page_content_response_complete(self):
        """Test page content mapping"""
        from app.schemas.esim_hub import ContentResponse

        content_response = Mock(spec=ContentResponse)
        category_detail = Mock()
        category_detail.name = "Privacy Policy"

        content_response.contentCategory = Mock()
        content_response.contentCategory.contentCategoryDetails = [category_detail]

        content_detail = Mock()
        content_detail.description = "This is our privacy policy..."
        content_response.contentDetails = [content_detail]

        result = DtoMapper.to_page_content_response(content_response)

        assert result.page_title == "Privacy Policy"
        assert result.page_content == "This is our privacy policy..."
        assert result.page_intro == ""


class TestDtoMapperToAuthResponse:
    """Test cases for to_auth_response method"""

    def test_to_auth_response_complete(self):
        """Test auth response mapping with complete data"""
        supabase_response = Mock(spec=AuthResponse)
        supabase_response.user = Mock()
        supabase_response.user.id = "user-123"
        supabase_response.user.email = "test@example.com"
        supabase_response.user.user_metadata = {
            "first_name": "John",
            "last_name": "Doe",
            "email": "test@example.com",
            "msisdn": "+1234567890",
            "email_verified": True,
            "should_notify": True,
            "referral_code": "REF123",
            "currency": "USD",
            "login_type": "email",
            "language": "En"
        }
        supabase_response.session = Mock()
        supabase_response.session.access_token = "access_token_123"
        supabase_response.session.refresh_token = "refresh_token_123"

        user_wallet = Mock()
        user_wallet.balance = 100.0

        with patch.dict(os.environ, {"DEFAULT_CURRENCY": "USD"}):
            result = DtoMapper.to_auth_response(supabase_response, user_wallet, currency="USD")

        assert isinstance(result, AuthResponseDTO)
        assert result.access_token == "access_token_123"
        assert result.refresh_token == "refresh_token_123"
        assert result.user_token == "user-123"
        assert result.is_verified is True
        assert result.user_info.first_name == "John"
        assert result.user_info.last_name == "Doe"
        assert result.user_info.balance == 100.0

    def test_to_auth_response_with_full_name_split(self):
        """Test splitting full_name into first and last name"""
        supabase_response = Mock(spec=AuthResponse)
        supabase_response.user = Mock()
        supabase_response.user.id = "user-456"
        supabase_response.user.email = "jane@example.com"
        supabase_response.user.user_metadata = {
            "full_name": "Jane Smith",
            "first_name": "",
            "last_name": "",
            "email": "jane@example.com"
        }
        supabase_response.session = Mock()
        supabase_response.session.access_token = "token"
        supabase_response.session.refresh_token = "refresh"

        result = DtoMapper.to_auth_response(supabase_response)

        assert result.user_info.first_name == "Jane"
        assert result.user_info.last_name == "Smith"

    def test_to_auth_response_without_session(self):
        """Test auth response without session (e.g., user not logged in)"""
        supabase_response = Mock(spec=AuthResponse)
        supabase_response.user = Mock()
        supabase_response.user.id = "user-789"
        supabase_response.user.email = "test@example.com"
        supabase_response.user.user_metadata = {
            "email": "test@example.com"
        }
        # No session attribute

        result = DtoMapper.to_auth_response(supabase_response)

        assert result.access_token == ""
        assert result.refresh_token == ""
        assert result.user_token == "user-789"

    def test_to_auth_response_email_masking_for_phone_login(self):
        """Test email display when logged in via phone"""
        supabase_response = Mock(spec=AuthResponse)
        supabase_response.user = Mock()
        supabase_response.user.id = "user-phone"
        supabase_response.user.email = "+1234567890@phone.supabase.co"
        supabase_response.user.user_metadata = {
            "msisdn": "+1234567890",
            "display_email": "real@example.com",
            "email": "+1234567890@phone.supabase.co"
        }
        supabase_response.session = Mock()
        supabase_response.session.access_token = "token"
        supabase_response.session.refresh_token = "refresh"

        result = DtoMapper.to_auth_response(supabase_response)

        # Should use display_email when email starts with msisdn
        assert result.user_info.email == "real@example.com"

    def test_to_auth_response_editability_flags(self):
        """Test email/phone editability based on login type"""
        # Email login - email not editable
        supabase_response = Mock(spec=AuthResponse)
        supabase_response.user = Mock()
        supabase_response.user.id = "user-email"
        supabase_response.user.email = "test@example.com"
        supabase_response.user.user_metadata = {
            "email": "test@example.com",
            "login_type": "email"
        }
        supabase_response.session = Mock()
        supabase_response.session.access_token = "token"
        supabase_response.session.refresh_token = "refresh"

        result = DtoMapper.to_auth_response(supabase_response)

        assert result.user_info.email_editable is False
        assert result.user_info.phone_editable is True


class TestDtoMapperToUserWalletResponse:
    """Test cases for to_user_wallet_response method"""

    def test_to_user_wallet_response(self):
        """Test wallet response mapping"""
        user_wallet = Mock(spec=UserWalletModel)
        user_wallet.amount = 150.75
        user_wallet.currency = "EUR"

        result = DtoMapper.to_user_wallet_response(user_wallet)

        assert result.balance == 150.75
        assert result.currency == "EUR"


class TestDtoMapperBundleCurrencyUpdate:
    """Test cases for bundle_currency_update method"""

    def test_bundle_currency_update_normal_display(self):
        """Test currency update with normal price display"""
        bundle = Mock(spec=BundleDTO)
        bundle.original_price = 10.0
        bundle.currency_code = "USD"
        bundle.price = 10.0
        bundle.price_display = "10.00 USD"

        with patch.dict(os.environ, {"DISPLAY_PRICE": "normal"}):
            result = DtoMapper.bundle_currency_update(bundle, currency="EUR", rate=0.85)

        assert result.currency_code == "EUR"
        assert result.price == 8.5
        assert result.price_display == "8.50 EUR"

    def test_bundle_currency_update_rounded_display(self):
        """Test currency update with rounded price display"""
        bundle = Mock(spec=BundleDTO)
        bundle.original_price = 10.75
        bundle.currency_code = "USD"
        bundle.price = 10.75
        bundle.price_display = "10.75 USD"

        with patch.dict(os.environ, {"DISPLAY_PRICE": "rounded"}):
            result = DtoMapper.bundle_currency_update(bundle, currency="GBP", rate=0.80)

        # 10.75 * 0.80 = 8.6, rounded up to 9
        assert result.price == 9
        assert result.currency_code == "GBP"


class TestDtoMapperToCurrencyDto:
    """Test cases for to_currency_dto method"""

    def test_to_currency_dto(self):
        """Test currency DTO mapping"""
        from app.models.app import CurrencyModel

        currency = Mock(spec=CurrencyModel)
        currency.name = "USD"

        result = DtoMapper.to_currency_dto(currency)

        assert result.currency == "USD"


class TestDtoMapperToPromotionHistoryDto:
    """Test cases for to_promotion_history_dto method"""

    def test_to_promotion_history_dto_with_referral(self):
        """Test promotion history with referral code"""
        promotion_usage = Mock(spec=PromotionUsageModel)
        promotion_usage.referral_code = "REF123"
        promotion_usage.amount = 10.0
        promotion_usage.referred_to = "John Doe"
        promotion_usage.created_at = "2024-01-01T00:00:00"

        result = DtoMapper.to_promotion_history_dto(
            promotion_usage,
            name="Referral Bonus",
            promotion_name="Friend Referral",
            rate=0.85,
            currency="EUR"
        )

        assert result.is_referral is True
        assert result.name == "John Doe"
        assert result.promotion_name == "Friend Referral"
        assert "8.50 EUR" in result.amount

    def test_to_promotion_history_dto_without_referral(self):
        """Test promotion history without referral"""
        promotion_usage = Mock(spec=PromotionUsageModel)
        promotion_usage.referral_code = None
        promotion_usage.amount = 15.0
        promotion_usage.referred_to = "Promo User"
        promotion_usage.created_at = "2026-01-01T00:00:00Z"

        result = DtoMapper.to_promotion_history_dto(
            promotion_usage,
            name="Special Offer",
            promotion_name="Summer Sale",
            rate=1.0,
            currency="USD"
        )

        assert result.is_referral is False
        assert "15.00 USD" in result.amount


class TestDtoMapperToExchangeRate:
    """Test cases for to_exchange_rate method"""

    def test_to_exchange_rate_complete(self):
        """Test exchange rate mapping"""
        data = {
            "systemCurrencyCode": "USD",
            "currencyCode": "EUR",
            "currentRate": "0.85",
            "newRate": "0.86"
        }

        result = DtoMapper.to_exchange_rate(data)

        assert result.system_currency_code == "USD"
        assert result.currency_code == "EUR"
        assert result.current_rate == 0.85
        assert result.new_rate == 0.86




# Integration and edge case tests
class TestDtoMapperIntegration:
    """Integration tests for complex scenarios"""

    def test_full_esim_purchase_flow(self):
        """Test complete eSIM purchase data flow"""
        # This would test the full flow from bundle selection to order creation
        pass

    def test_currency_conversion_consistency(self):
        """Test that currency conversions are consistent across all methods"""
        pass

    def test_date_handling_across_timezones(self):
        """Test date handling with different timezone scenarios"""
        pass


# Performance tests
class TestDtoMapperPerformance:
    """Performance-related tests"""

    @pytest.mark.skip(reason="Performance test - run separately")
    def test_bulk_bundle_mapping_performance(self):
        """Test performance with large number of bundles"""
        pass

