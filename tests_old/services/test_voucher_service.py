import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.exceptions import CustomException
from app.models.user import UserModel
from app.schemas.voucher import VoucherRequestRedeem
from app.services.voucher_service import VoucherService


@pytest.fixture
def voucher_service():
    with patch('app.services.voucher_service.VoucherRepo') as MockVoucherRepo, \
         patch('app.services.voucher_service.UserWalletService') as MockUserWalletService, \
         patch('app.services.voucher_service.CurrencyService') as MockCurrencyService:
        service = VoucherService()
        service._VoucherService__voucher_repo = MockVoucherRepo()
        service._VoucherService__user_wallet_service = AsyncMock()
        service._VoucherService__currency_service = MockCurrencyService()
        return service


@pytest.mark.asyncio
async def test_redeem_success(voucher_service):
    mock_voucher = MagicMock()
    mock_voucher.id = 1
    mock_voucher.amount = 10.0
    mock_voucher.is_used = False
    mock_voucher.used_by = None
    mock_voucher.expired_at = None

    # Mock the first call to check if voucher is already used (should return None)
    # Mock the second call to get the actual voucher (should return mock_voucher)
    voucher_service._VoucherService__voucher_repo.get_first_by.side_effect = [None, mock_voucher]
    voucher_service._VoucherService__user_wallet_service.add_wallet_transaction = AsyncMock()
    voucher_service._VoucherService__voucher_repo.update_by = MagicMock()
    voucher_service._VoucherService__currency_service.get_rate_by_currency.return_value = 1.0

    user = UserModel(id="user123", email="test@example.com", token="token", msisdn="", is_verified=True)
    redeem_request = VoucherRequestRedeem(code="TEST123")

    with patch('app.services.voucher_service.ResponseHelper.success_response') as mock_resp:
        mock_resp.return_value = MagicMock(status="success")
        result = await voucher_service.redeem(redeem_request, user, "USD")
        voucher_service._VoucherService__voucher_repo.update_by.assert_called_with(
            where={'id': 1},
            data={'used_by': 'user123', 'is_used': True}
        )
        mock_resp.assert_called_once()


@pytest.mark.asyncio
async def test_redeem_voucher_not_found(voucher_service):
    # Mock the first call to check if voucher is already used (should return None)
    # Mock the second call to get the actual voucher (should also return None)
    voucher_service._VoucherService__voucher_repo.get_first_by.side_effect = [None, None]

    user = UserModel(id="user123", email="test@example.com", token="token", msisdn="", is_verified=True)
    redeem_request = VoucherRequestRedeem(code="INVALID")

    with pytest.raises(CustomException):
        await voucher_service.redeem(redeem_request, user, "USD")


@pytest.mark.asyncio
async def test_redeem_voucher_already_used(voucher_service):
    mock_voucher = MagicMock()
    mock_voucher.is_used = True
    mock_voucher.used_by = "other_user"

    voucher_service._VoucherService__voucher_repo.get_first_by.return_value = mock_voucher

    user = UserModel(id="user123", email="test@example.com", token="token", msisdn="", is_verified=True)
    redeem_request = VoucherRequestRedeem(code="USED123")

    with pytest.raises(CustomException):
        await voucher_service.redeem(redeem_request, user, "USD")


@pytest.mark.asyncio
async def test_redeem_wallet_transaction_failure(voucher_service):
    mock_voucher = MagicMock()
    mock_voucher.id = 1
    mock_voucher.amount = 10.0
    mock_voucher.is_used = False
    mock_voucher.used_by = None
    mock_voucher.expired_at = None

    # Mock the first call to check if voucher is already used (should return None)
    # Mock the second call to get the actual voucher (should return mock_voucher)
    voucher_service._VoucherService__voucher_repo.get_first_by.side_effect = [None, mock_voucher]
    voucher_service._VoucherService__user_wallet_service.add_wallet_transaction.side_effect = Exception("Wallet error")
    voucher_service._VoucherService__currency_service.get_rate_by_currency.return_value = 1.0

    user = UserModel(id="user123", email="test@example.com", token="token", msisdn="", is_verified=True)
    redeem_request = VoucherRequestRedeem(code="TEST123")

    with patch('app.services.voucher_service.logger') as mock_logger:
        with pytest.raises(CustomException):
            await voucher_service.redeem(redeem_request, user, "USD")
        mock_logger.error.assert_called()
