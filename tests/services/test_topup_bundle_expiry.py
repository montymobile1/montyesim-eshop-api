"""Test for TopUp Bundle Expiry Logic - Expire All Bundles with Same ICCID"""
import pytest
from unittest.mock import Mock, patch, AsyncMock, MagicMock

from app.services.callback_service import CallbackService
from app.config.db import UserBundleType
from app.models.user import UserProfileBundleModel


class MockRequest:
    """Mock FastAPI Request object"""
    def __init__(self, json_data):
        self._json_data = json_data
        self.headers = {}
        self.scope = {"type": "http", "method": "POST", "path": "/"}

    async def json(self):
        return self._json_data


@pytest.fixture
def mock_user_repo():
    with patch('app.services.callback_service.UserRepo') as mock:
        yield mock


@pytest.fixture
def mock_user_order_repo():
    with patch('app.services.callback_service.UserOrderRepo') as mock:
        yield mock


@pytest.fixture
def mock_user_profile_repo():
    with patch('app.services.callback_service.UserProfileRepo') as mock:
        yield mock


@pytest.fixture
def mock_user_profile_bundle_repo():
    with patch('app.services.callback_service.UserProfileBundleRepo') as mock:
        yield mock


@pytest.fixture
def mock_sync_service():
    with patch('app.services.callback_service.SyncService') as mock:
        mock_instance = mock.return_value
        mock_instance.sync_bundles = AsyncMock()
        mock_instance.sync_bundle = AsyncMock()
        mock_instance.update_sync_version = Mock()
        yield mock


@pytest.fixture
def mock_wallet_service():
    with patch('app.services.callback_service.UserWalletService') as mock:
        yield mock


@pytest.fixture
def mock_promotion_service():
    with patch('app.services.callback_service.PromotionService') as mock:
        yield mock


@pytest.fixture
def mock_bundle_service():
    with patch('app.services.callback_service.BundleService') as mock:
        yield mock


@pytest.fixture
def mock_fcm_service():
    with patch('app.services.callback_service.fcm_service') as mock:
        yield mock


@pytest.fixture
def mock_esim_hub_service():
    with patch('app.services.callback_service.esim_hub_service_instance') as mock:
        mock_instance = mock.return_value
        mock_instance.get_bundle_by_id = AsyncMock(return_value={})
        yield mock


@pytest.fixture
def callback_service(mock_user_repo, mock_user_order_repo, mock_user_profile_repo,
                     mock_user_profile_bundle_repo, mock_sync_service, mock_wallet_service,
                     mock_promotion_service, mock_bundle_service, mock_fcm_service, mock_esim_hub_service):
    return CallbackService()


class TestTopupBundleExpiry:
    """Test suite for TopUp Bundle expiry and cascading expiry to Primary bundle"""

    def test_is_topup_expired_event_with_expired_status(self, callback_service):
        """
        GIVEN: An event with status='EXPIRED'
        WHEN: __is_topup_expired_event is called
        THEN: It should return True
        """
        # Arrange
        payload = {"status": "EXPIRED"}

        # Act
        result = callback_service._CallbackService__is_topup_expired_event("SOME_EVENT", payload)

        # Assert
        assert result is True

    def test_is_topup_expired_event_with_bundle_expired_flag(self, callback_service):
        """
        GIVEN: An event with bundle_expired=True
        WHEN: __is_topup_expired_event is called
        THEN: It should return True
        """
        # Arrange
        payload = {"bundle_expired": True}

        # Act
        result = callback_service._CallbackService__is_topup_expired_event("SOME_EVENT", payload)

        # Assert
        assert result is True

    def test_is_topup_expired_event_with_explicit_event_type(self, callback_service):
        """
        GIVEN: An event with explicit expiry event type
        WHEN: __is_topup_expired_event is called with TOPUP_EXPIRED
        THEN: It should return True
        """
        # Arrange
        payload = {"other_field": "value"}

        # Act
        result = callback_service._CallbackService__is_topup_expired_event("TOPUP_EXPIRED", payload)

        # Assert
        assert result is True

    def test_is_topup_expired_event_with_prepaid_completion(self, callback_service):
        """
        GIVEN: An event with PREPAID_PLAN_COMPLETION type
        WHEN: __is_topup_expired_event is called
        THEN: It should return True (explicit expiry event)
        """
        # Arrange
        payload = {}

        # Act
        result = callback_service._CallbackService__is_topup_expired_event("PREPAID_PLAN_COMPLETION", payload)

        # Assert
        assert result is True

    def test_force_expire_all_bundles_by_iccid(self, callback_service, mock_user_profile_bundle_repo):
        """
        GIVEN: An ICCID with multiple bundles (active)
        WHEN: __force_expire_all_bundles_by_iccid is called
        THEN: It should update all non-expired bundles to expired and return count

        This tests the core fix: when TopUp expires, all bundles with same ICCID expire.
        """
        # Arrange
        iccid = "test_iccid_expiry"
        mock_user_profile_bundle_repo.return_value.update_by.return_value = 2  # 2 bundles expired

        # Act
        rows = callback_service._CallbackService__force_expire_all_bundles_by_iccid(iccid)

        # Assert
        assert rows == 2
        # Verify that update_by was called with correct parameters
        mock_user_profile_bundle_repo.return_value.update_by.assert_called_once()
        call_kwargs = mock_user_profile_bundle_repo.return_value.update_by.call_args[1]
        assert call_kwargs['where'] == {"iccid": iccid, "bundle_expired": False}
        assert call_kwargs['data'] == {"bundle_expired": True}




