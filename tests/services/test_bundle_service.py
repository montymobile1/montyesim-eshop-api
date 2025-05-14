import os
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.schemas.response import ResponseHelper
from app.services.bundle_service import BundleService  # Import the class you're testing
from tests.mocks import get_bundle_mock, get_bundle_mocks, get_region_mocks


@pytest.fixture
def mock_bundle_repo(mocker):
    repo = mocker.patch("app.services.bundle_service.BundleRepo")
    return repo.return_value


@pytest.fixture
def mock_tag_repo(mocker):
    repo = mocker.patch("app.services.bundle_service.TagRepo")
    return repo.return_value


@pytest.fixture
def mock_grouping_service(mocker):
    service = mocker.patch("app.services.bundle_service.GroupingService")
    return service.return_value


@pytest.fixture
def mock_esim_hub_service(mocker):
    service = mocker.patch("app.services.bundle_service.esim_hub_service_instance")
    return service.return_value


@pytest.fixture
def service(mock_esim_hub_service, mock_grouping_service, mock_bundle_repo, mock_tag_repo):
    return BundleService()


class TestBundleService(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DEFAULT_CURRENCY"] = "EUR"
        os.environ["SUPABASE_URL"] = "None"

    def setUp(self):
        patcher_esim = patch("app.services.bundle_service.esim_hub_service_instance")
        patcher_grouping = patch("app.services.bundle_service.GroupingService")
        patcher_bundle_repo = patch("app.services.bundle_service.BundleRepo")
        patcher_tag_repo = patch("app.services.bundle_service.TagRepo")

        self.addCleanup(patcher_esim.stop)
        self.addCleanup(patcher_grouping.stop)
        self.addCleanup(patcher_bundle_repo.stop)
        self.addCleanup(patcher_tag_repo.stop)

        self.mock_esim = patcher_esim.start().return_value
        self.mock_grouping = patcher_grouping.start().return_value
        self.mock_bundle_repo = patcher_bundle_repo.start().return_value
        self.mock_tag_repo = patcher_tag_repo.start().return_value

        self.service = BundleService()

    # async def test_get_bundle(self):
    #     self.mock_bundle_repo.get_bundle_by_id.return_value= get_bundle_mock()
    #     response = await self.service.get_bundle("123","EUR")
    #
    #     self.assertEqual(response.data,get_bundle_mock())

    # async def test_get_regions(self):
    #     self.mock_grouping.get_all_regions = AsyncMock(return_value=MagicMock(get_region_mocks()))
    #     response = await self.service.get_regions()
    #     self.assertEqual(response.data, get_region_mocks())
    #
    # async def test_get_bundles_by_country(self):
    #     self.mock_tag_repo.get_first_by = AsyncMock(return_value=MagicMock(id="tag1"))
    #     self.mock_tag_repo.select = MagicMock(return_value=["bundle1", "bundle2"])
    #     self.mock_bundle_repo.select = MagicMock(return_value=get_region_mocks())
    #
    #     response = await self.service.get_bundles_by_country("US")
    #     self.assertEqual(response.count, 2)
    #     self.assertIsInstance(response.data[0], BundleDTO)
    #
    # async def test_get_bundles_by_country_empty(self):
    #     with self.assertRaises(BadRequestException):
    #         await self.service.get_bundles_by_country("")
    #
    # async def test_get_bundles_by_country_not_found(self):
    #     self.mock_tag_repo.get_first_by = AsyncMock(return_value=None)
    #     with self.assertRaises(BadRequestException):
    #         await self.service.get_bundles_by_country("ZZ")
    #
    # async def test_get_bundles_by_region(self):
    #     self.mock_grouping.get_all_regions = AsyncMock(return_value=get_region_mocks())
    #     self.mock_tag_repo.select = MagicMock(return_value=["bundle1", "bundle2"])
    #     self.mock_bundle_repo.select = MagicMock(return_value=get_bundle_mocks())
    #
    #     response = await self.service.get_bundles_by_region("MENA", "USD")
    #     self.assertEqual(response.count, 1)
    #     self.assertEqual(response.data[0].bundle_code, "bundle2")
    #
    # async def test_get_bundles_by_region_not_found(self):
    #     self.mock_grouping.get_all_regions = AsyncMock(return_value=[])
    #     with self.assertRaises(BadRequestException):
    #         await self.service.get_bundles_by_region("UNKNOWN", "USD")
    #
    # async def test_get_countries(self):
    #     self.mock_grouping.get_all_countries = AsyncMock(return_value=get_country_mocks())
    #     response = await self.service.get_countries()
    #     self.assertEqual(response.count, len(get_country_mocks()))
    #     self.assertEqual(response.data, get_country_mocks())


if __name__ == "__main__":
    unittest.main()
