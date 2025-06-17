import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.user_wallet_service import UserWalletService

@pytest.fixture
def user_wallet_service():
    with patch('app.services.user_wallet_service.UserWalletRepo') as MockWalletRepo, \
         patch('app.services.user_wallet_service.UserOrderRepo') as MockOrderRepo, \
         patch('app.services.user_wallet_service.UserWalletTransactionRepo') as MockTransRepo, \
         patch('app.services.user_wallet_service.CurrencyService') as MockCurrencyService:
        service = UserWalletService()
        service._UserWalletService__user_wallet_repo = MockWalletRepo()
        service._UserWalletService__user_order_repo = MockOrderRepo()
        service._UserWalletService__user_wallet_transaction_repo = MockTransRepo()
        service._UserWalletService__currency_service = MockCurrencyService()
        return service

def test_get_user_wallet_by_id_found(user_wallet_service):
    mock_wallet = MagicMock()
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = mock_wallet
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        result = asyncio.run(user_wallet_service.get_user_wallet_by_id('id1'))
        assert result == 'dto'
        mock_mapper.assert_called_once()

def test_get_user_wallet_by_id_not_found(user_wallet_service):
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = None
    result = asyncio.run(user_wallet_service.get_user_wallet_by_id('id1'))
    assert result is None

def test_create_wallet(user_wallet_service):
    mock_wallet = MagicMock()
    user_wallet_service._UserWalletService__user_wallet_repo.create.return_value = mock_wallet
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        dto = asyncio.run(user_wallet_service.create_wallet(MagicMock(user_id='u', amount=1, currency='USD')))
        assert dto == 'dto'
        mock_mapper.assert_called_once()

def test_get_user_wallet_by_user_id_found(user_wallet_service):
    mock_wallet = MagicMock(currency='USD', amount=10)
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = mock_wallet
    user_wallet_service._UserWalletService__currency_service.get_currency_rate.return_value = 2
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        dto = asyncio.run(user_wallet_service.get_user_wallet_by_user_id('uid', 'EUR'))
        assert dto == 'dto'
        mock_mapper.assert_called_once()

def test_get_user_wallet_by_user_id_not_found(user_wallet_service):
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = None
    result = asyncio.run(user_wallet_service.get_user_wallet_by_user_id('uid', 'EUR'))
    assert result is None

def test_add_wallet_transaction_success(user_wallet_service):
    mock_wallet = MagicMock(amount=10, id='wid', currency='USD')
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = mock_wallet
    user_wallet_service._UserWalletService__user_wallet_repo.update_by = MagicMock()
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.create = MagicMock()
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        with patch('app.services.user_wallet_service.ResponseHelper.success_data_response', return_value='resp') as mock_resp:
            resp = asyncio.run(user_wallet_service.add_wallet_transaction(5, 'uid', 'voucher'))
            assert resp == 'resp'
            mock_mapper.assert_called_once()
            mock_resp.assert_called_once()

def test_add_wallet_transaction_wallet_not_found(user_wallet_service):
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = None
    with pytest.raises(Exception):
        asyncio.run(user_wallet_service.add_wallet_transaction(5, 'uid', 'voucher'))
