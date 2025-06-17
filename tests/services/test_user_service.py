import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.user_service import UserBundleService

@pytest.fixture
def user_bundle_service():
    with patch('app.services.user_service.esim_hub_service_instance') as mock_esim_hub, \
         patch('app.services.user_service.NotificationRepo') as mock_notif, \
         patch('app.services.user_service.UserOrderRepo') as mock_order, \
         patch('app.services.user_service.UserProfileRepo') as mock_profile, \
         patch('app.services.user_service.UserProfileBundleRepo') as mock_profile_bundle, \
         patch('app.services.user_service.BundleRepo') as mock_bundle_repo, \
         patch('app.services.user_service.UserWalletService') as mock_wallet, \
         patch('app.services.user_service.PromotionService') as mock_promo, \
         patch('app.services.user_service.BundleService') as mock_bundle_service, \
         patch('app.services.user_service.dcb_service_instance') as mock_dcb:
        service = UserBundleService()
        service._UserBundleService__esim_hub_service = AsyncMock()
        service._UserBundleService__notification_repo = mock_notif()
        service._UserBundleService__user_order_repo = mock_order()
        service._UserBundleService__user_profile_repo = mock_profile()
        service._UserBundleService__user_profile_bundle_repo = mock_profile_bundle()
        service._UserBundleService__bundle_repo = mock_bundle_repo()
        service._UserBundleService__user_wallet_service = mock_wallet()
        service._UserBundleService__promotion_service = mock_promo()
        service._UserBundleService__bundle_service = mock_bundle_service()
        service._UserBundleService__dcb_service = mock_dcb()
        return service

def test_user_bundle_service_init(user_bundle_service):
    assert user_bundle_service is not None
