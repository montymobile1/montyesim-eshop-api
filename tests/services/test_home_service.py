import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from app.schemas.home import HomeResponseDto
from app.schemas.response import Response
from app.services.home_service import HomeService
from tests.mocks import get_country_mocks, get_region_mocks, get_bundle_mocks


class TestHomeService(unittest.IsolatedAsyncioTestCase):

    @patch("app.services.home_service.esim_hub_service_instance")
    def setUp(self, mock_esim_hub_service):
        self.mock_esim_hub_service = mock_esim_hub_service.return_value
        self.mock_esim_hub_service.get_countries = AsyncMock(return_value=get_country_mocks())
        self.mock_esim_hub_service.get_regions = AsyncMock(return_value=get_region_mocks())
        self.mock_esim_hub_service.get_bundles_by_category = AsyncMock(return_value=get_bundle_mocks())

        self.home_service = HomeService()
        self.home_service._HomeService__esim_hub_service = self.mock_esim_hub_service  # Mock private attribute

    async def test_home(self):
        response = await self.home_service.home()

        self.mock_esim_hub_service.get_countries.assert_called_once()
        self.mock_esim_hub_service.get_regions.assert_called_once()
        self.mock_esim_hub_service.get_bundles_by_category.assert_any_call(category="CRUISE")
        self.mock_esim_hub_service.get_bundles_by_category.assert_any_call(category="GLOBAL")

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")

        home_data = response.data
        self.assertIsInstance(home_data, HomeResponseDto)
        self.assertEqual(home_data.countries, get_country_mocks())
        self.assertEqual(home_data.regions, get_region_mocks())
        self.assertEqual(home_data.cruise_bundles, get_bundle_mocks())
        self.assertEqual(home_data.global_bundles, get_bundle_mocks())

    async def test_get_cruise_bundles_populates_all_sections(self):
        self.home_service._HomeService__get_countries_v2 = AsyncMock(return_value=get_country_mocks())
        self.home_service._HomeService__get_regions_v2 = AsyncMock(return_value=get_region_mocks())
        self.home_service._HomeService__currency_service = MagicMock()
        self.home_service._HomeService__currency_service.get_rate_by_currency.return_value = 1.0
        self.home_service._HomeService__grouping_service = MagicMock()
        self.home_service._HomeService__grouping_service.get_cruise_bundle = AsyncMock(return_value=get_bundle_mocks())
        self.home_service._HomeService__grouping_service.get_global_bundle = AsyncMock(return_value=get_bundle_mocks())

        response = await self.home_service.get_cruise_bundles(currency="USD", locale="en")

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.data.countries, get_country_mocks())
        self.assertEqual(response.data.regions, get_region_mocks())
        self.assertEqual(response.data.cruise_bundles, get_bundle_mocks())
        self.assertEqual(response.data.global_bundles, get_bundle_mocks())


if __name__ == "__main__":
    unittest.main()
