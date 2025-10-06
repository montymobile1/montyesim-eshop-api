import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from app.services.grouping_service import GroupingService

@pytest.fixture
def grouping_service():
    with patch('app.services.grouping_service.TagRepo') as MockTagRepo, \
         patch('app.services.grouping_service.BundleTagRepo') as MockBundleTagRepo, \
         patch('app.services.grouping_service.BundleRepo') as MockBundleRepo, \
         patch('app.services.grouping_service.TagTranslationRepo') as MockTagTranslationRepo:
        service = GroupingService()
        service._GroupingService__tag_repo = MockTagRepo()
        service._GroupingService__bundle_tag_repo = MockBundleTagRepo()
        service._GroupingService__bundle_repo = MockBundleRepo()
        service._GroupingService__tag_translation_repo = MockTagTranslationRepo()
        return service

@pytest.mark.asyncio
async def test_get_all_countries(grouping_service):
    mock_tag = MagicMock()
    mock_tag.name = 'CountryA'
    mock_tag.data = {'code': 'A'}
    grouping_service._GroupingService__tag_repo.select_procedure.return_value = [mock_tag]
    with patch('app.services.grouping_service.CountryDTO.model_validate', return_value='dto') as mock_validate:
        result = await grouping_service.get_all_countries('en')
        assert result == ['dto']
        mock_validate.assert_called_once()

@pytest.mark.asyncio
async def test_get_all_regions(grouping_service):
    mock_tag = MagicMock()
    mock_tag.name = 'RegionA'
    mock_tag.data = {'code': 'A'}
    grouping_service._GroupingService__tag_repo.select_procedure.return_value = [mock_tag]
    with patch('app.services.grouping_service.RegionDTO.model_validate', return_value='dto') as mock_validate:
        result = await grouping_service.get_all_regions('en')
        assert result == ['dto']
        mock_validate.assert_called_once()

@pytest.mark.asyncio
async def test_get_cruise_bundle(grouping_service):
    mock_tag = MagicMock()
    mock_tag.id = 1
    grouping_service._GroupingService__tag_repo.select_procedure.return_value = [mock_tag]

    mock_bundle_tag = MagicMock()
    mock_bundle_tag.bundle_id = 10
    grouping_service._GroupingService__bundle_tag_repo.list.return_value = [mock_bundle_tag]

    mock_bundle = MagicMock()
    mock_bundle.data = {'countries': [{'id': 1}], 'other': 'data'}
    grouping_service._GroupingService__bundle_repo.list.return_value = [mock_bundle]

    with patch('app.services.grouping_service.BundleDTO') as mock_bundle_dto, \
         patch('app.services.grouping_service.DtoMapper.bundle_currency_update') as mock_update, \
         patch('os.getenv', return_value='en'):

        mock_bundle_dto_instance = MagicMock()
        mock_bundle_dto_instance.countries = [MagicMock(id=1)]
        mock_bundle_dto.return_value = mock_bundle_dto_instance
        mock_update.return_value = mock_bundle_dto_instance

        result = await grouping_service.get_cruise_bundle(1.0, 'USD', 'en')
        assert len(result) == 1
        mock_update.assert_called_with(mock_bundle_dto_instance, 'USD', 1.0)

@pytest.mark.asyncio
async def test_get_global_bundle(grouping_service):
    mock_tag = MagicMock()
    mock_tag.id = 1
    grouping_service._GroupingService__tag_repo.select_procedure.return_value = [mock_tag]

    mock_bundle_tag = MagicMock()
    mock_bundle_tag.bundle_id = 10
    grouping_service._GroupingService__bundle_tag_repo.list.return_value = [mock_bundle_tag]

    mock_bundle = MagicMock()
    mock_bundle.data = {'countries': [{'id': 1}], 'other': 'data'}
    grouping_service._GroupingService__bundle_repo.list.return_value = [mock_bundle]

    with patch('app.services.grouping_service.BundleDTO') as mock_bundle_dto, \
         patch('app.services.grouping_service.DtoMapper.bundle_currency_update') as mock_update, \
         patch('os.getenv', return_value='en'):

        mock_bundle_dto_instance = MagicMock()
        mock_bundle_dto_instance.countries = [MagicMock(id=1)]
        mock_bundle_dto.return_value = mock_bundle_dto_instance
        mock_update.return_value = mock_bundle_dto_instance

        result = await grouping_service.get_global_bundle(1.0, 'USD', 'en')
        assert len(result) == 1
        mock_update.assert_called_with(mock_bundle_dto_instance, 'USD', 1.0)

@pytest.mark.asyncio
async def test_translate_tags(grouping_service):
    mock_tag = MagicMock()
    mock_tag.id = 1
    mock_tag.name = 'TagName'
    mock_tag.data = {'foo': 'bar'}
    grouping_service._GroupingService__tag_repo.list.return_value = [mock_tag]

    # Mock that no existing translation exists
    grouping_service._GroupingService__tag_translation_repo.get_first_by.return_value = None
    grouping_service._GroupingService__tag_translation_repo.create = MagicMock()

    with patch('app.services.grouping_service.GoogleTranslator') as mock_translator:
        instance = mock_translator.return_value
        instance.translate.return_value = 'TranslatedName'

        await grouping_service.translate_tags('fr')

        instance.translate.assert_called_with('TagName')
        grouping_service._GroupingService__tag_translation_repo.create.assert_called_with({
            "tag_id": 1,
            "locale": 'fr',
            "name": 'TranslatedName',
            "data": {'foo': 'bar'}
        })
