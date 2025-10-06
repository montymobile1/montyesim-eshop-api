import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from app.services.sync_service import SyncService

@pytest.fixture
def sync_service():
    with patch('app.services.sync_service.EsimHubService') as MockEsimHubService, \
         patch('app.services.sync_service.BundleRepo') as MockBundleRepo, \
         patch('app.services.sync_service.TagRepo') as MockTagRepo, \
         patch('app.services.sync_service.BundleTagRepo') as MockBundleTagRepo, \
         patch('app.services.sync_service.ConfigRepo') as MockConfigRepo:
        service = SyncService()
        service._SyncService__esim_hub_service = AsyncMock()
        service._SyncService__bundle_repo = MockBundleRepo()
        service._SyncService__tag_repo = MockTagRepo()
        service._SyncService__bundle_tag_repo = MockBundleTagRepo()
        service._SyncService__config_repo = MockConfigRepo()
        return service

@pytest.mark.asyncio
async def test_sync_bundles(sync_service):
    mock_bundle = MagicMock()
    mock_bundle.countries = []
    mock_bundle.bundle_region = []
    mock_bundle.bundle_code = 'code1'
    all_bundle_response = MagicMock()
    all_bundle_response.total_rows = 1
    all_bundle_response.bundles = [mock_bundle]
    sync_service._SyncService__esim_hub_service.get_all_bundles.return_value = all_bundle_response
    sync_service.sync_bundle = AsyncMock()
    await sync_service.sync_bundles()
    sync_service.sync_bundle.assert_called()

@pytest.mark.asyncio
async def test_sync_bundle_create(sync_service):
    mock_bundle = MagicMock()
    mock_bundle.countries = []
    mock_bundle.bundle_region = []
    mock_bundle.bundle_code = 'code1'
    mock_bundle.model_dump.return_value = {}
    sync_service._SyncService__bundle_repo.get_by_id.return_value = None
    sync_service._SyncService__tag_repo.get_first_by.return_value = None
    sync_service._SyncService__tag_repo.create = MagicMock()
    sync_service._SyncService__bundle_repo.create = MagicMock()
    sync_service._SyncService__bundle_tag_repo.create = MagicMock()
    await sync_service.sync_bundle(mock_bundle)
    sync_service._SyncService__bundle_repo.create.assert_called()

@pytest.mark.asyncio
async def test_sync_bundle_update(sync_service):
    mock_bundle = MagicMock()
    mock_bundle.countries = []
    mock_bundle.bundle_region = []
    mock_bundle.bundle_code = 'code1'
    mock_bundle.model_dump.return_value = {}
    sync_service._SyncService__bundle_repo.get_by_id.return_value = True
    sync_service._SyncService__tag_repo.get_first_by.return_value = None
    sync_service._SyncService__bundle_repo.update = MagicMock()
    sync_service._SyncService__bundle_tag_repo.create = MagicMock()
    await sync_service.sync_bundle(mock_bundle)
    sync_service._SyncService__bundle_repo.update.assert_called()

@pytest.mark.asyncio
async def test_update_sync_version_create(sync_service):
    sync_service._SyncService__config_repo.get_first_by.return_value = None
    sync_service._SyncService__config_repo.create = MagicMock()
    await sync_service.update_sync_version()
    sync_service._SyncService__config_repo.create.assert_called()

@pytest.mark.asyncio
async def test_update_sync_version_update(sync_service):
    sync_service._SyncService__config_repo.get_first_by.return_value = True
    sync_service._SyncService__config_repo.update_by = MagicMock()
    await sync_service.update_sync_version()
    sync_service._SyncService__config_repo.update_by.assert_called()

@pytest.mark.asyncio
async def test_delete_bundle(sync_service):
    sync_service._SyncService__bundle_repo.update = MagicMock()

    await sync_service.delete_bundle("bundle123")
    # The delete_bundle method only deactivates the bundle, it doesn't delete tags
    sync_service._SyncService__bundle_repo.update.assert_called_with(record_id="bundle123", data={"is_active": False})

@pytest.mark.asyncio
async def test_update_bundle_status(sync_service):
    sync_service._SyncService__bundle_repo.update = MagicMock()
    await sync_service.update_bundle_status('bundle1', True)
    sync_service._SyncService__bundle_repo.update.assert_called_with(record_id='bundle1', data={'is_active': True})
