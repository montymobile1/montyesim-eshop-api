import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.voucher_service import VoucherService

@pytest.fixture
def voucher_service():
    with patch('app.services.voucher_service.VoucherRepo') as MockVoucherRepo, \
         patch('app.services.voucher_service.UserWalletService') as MockUserWalletService:
        service = VoucherService()
        service._VoucherService__voucher_repo = MockVoucherRepo()
        service._VoucherService__user_wallet_service = AsyncMock()
        return service

def test_redeem_success(voucher_service):
    mock_voucher = MagicMock(id=1, amount=10)
    voucher_service._VoucherService__voucher_repo.get_first_by.return_value = mock_voucher
    voucher_service._VoucherService__user_wallet_service.add_wallet_transaction.return_value = AsyncMock()
    voucher_service._VoucherService__voucher_repo.update_by = MagicMock()
    with patch('app.services.voucher_service.ResponseHelper.success_response', return_value='resp') as mock_resp:
        result = asyncio.run(voucher_service.redeem(MagicMock(code='code'), MagicMock(id='uid')))
        assert result == 'resp'
        voucher_service._VoucherService__voucher_repo.update_by.assert_called_with(where={'id': 1}, data={'used_by': 'uid', 'is_used': True})
        mock_resp.assert_called_once()

def test_redeem_voucher_not_found(voucher_service):
    voucher_service._VoucherService__voucher_repo.get_first_by.return_value = None
    with pytest.raises(Exception):
        asyncio.run(voucher_service.redeem(MagicMock(code='code'), MagicMock(id='uid')))

def test_redeem_wallet_fail(voucher_service):
    mock_voucher = MagicMock(id=1, amount=10)
    voucher_service._VoucherService__voucher_repo.get_first_by.return_value = mock_voucher
    voucher_service._VoucherService__user_wallet_service.add_wallet_transaction.side_effect = Exception('fail')
    with patch('app.services.voucher_service.logger') as mock_logger:
        with pytest.raises(Exception):
            asyncio.run(voucher_service.redeem(MagicMock(code='code'), MagicMock(id='uid')))
        assert mock_logger.error.called
