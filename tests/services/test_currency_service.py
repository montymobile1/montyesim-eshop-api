import pytest
from unittest.mock import MagicMock, patch
from app.services.currency_service import CurrencyService
from app.schemas.home import CurrencyDto
from app.schemas.response import ResponseHelper

@pytest.fixture
def currency_service():
    with patch('app.services.currency_service.CurrencyRepo') as MockRepo:
        service = CurrencyService()
        service._CurrencyService__currency_repo = MockRepo()
        return service

def test_get_rate_by_currency_system_currency(currency_service):
    with patch('os.getenv', return_value='USD'):
        assert currency_service.get_rate_by_currency('USD') == 1

def test_get_rate_by_currency_found(currency_service):
    mock_currency = MagicMock(rate=3)
    currency_service._CurrencyService__currency_repo.get_first_by.return_value = mock_currency
    with patch('os.getenv', return_value='USD'):
        assert currency_service.get_rate_by_currency('EUR') == 3

def test_get_rate_by_currency_not_found(currency_service):
    currency_service._CurrencyService__currency_repo.get_first_by.return_value = None
    with patch('os.getenv', return_value='USD'):
        assert currency_service.get_rate_by_currency('EUR') == 1

def test_get_currency_rate_found(currency_service):
    mock_currency = MagicMock(rate=2.0)
    currency_service._CurrencyService__currency_repo.get_first_by.return_value = mock_currency
    assert currency_service.get_currency_rate('USD', 'EUR') == 2

def test_get_currency_rate_not_found(currency_service):
    currency_service._CurrencyService__currency_repo.get_first_by.return_value = None
    assert currency_service.get_currency_rate('USD', 'EUR') == 1

def test_get_all_currency(currency_service):
    mock_currency = MagicMock()
    with patch('app.services.currency_service.DtoMapper.to_currency_dto', return_value='dto') as mock_mapper:
        currency_service._CurrencyService__currency_repo.list.return_value = [mock_currency, mock_currency]
        resp = currency_service.get_all_currency()
        assert resp.status == 'success'
        assert resp.totalCount == 2
        assert resp.data == ['dto', 'dto']
        mock_mapper.assert_called()
