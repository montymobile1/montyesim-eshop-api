# import pytest
# from unittest.mock import Mock, patch, AsyncMock, MagicMock
# from fastapi import Request
# from app.services.callback_service import CallbackService
# from app.exceptions import CustomException
# from datetime import datetime
# from app.schemas.callback import ConsumptionLimitRequest, NotificationCategoryType
# import json
#
# class MockRequest:
#     def __init__(self, json_data):
#         self._json_data = json_data
#         self.headers = {}
#         self.scope = {"type": "http", "method": "POST", "path": "/"}
#
#     async def json(self):
#         return self._json_data
#
#     async def body(self):
#         return json.dumps(self._json_data).encode()
#
# @pytest.fixture
# def mock_user_repo():
#     with patch('app.services.callback_service.UserRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_user_order_repo():
#     with patch('app.services.callback_service.UserOrderRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_user_profile_repo():
#     with patch('app.services.callback_service.UserProfileRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_user_profile_bundle_repo():
#     with patch('app.services.callback_service.UserProfileBundleRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_sync_service():
#     with patch('app.services.callback_service.SyncService') as mock:
#         mock_instance = mock.return_value
#         mock_instance.sync_bundles = AsyncMock()
#         mock_instance.sync_bundle = AsyncMock()
#         mock_instance.update_sync_version = AsyncMock()
#         yield mock
#
# @pytest.fixture
# def mock_wallet_service():
#     with patch('app.services.callback_service.UserWalletService') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_promotion_service():
#     with patch('app.services.callback_service.PromotionService') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_bundle_service():
#     with patch('app.services.callback_service.BundleService') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_fcm_service():
#     with patch('app.services.callback_service.fcm_service') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_esim_hub_service():
#     with patch('app.services.callback_service.esim_hub_service_instance') as mock:
#         mock_instance = mock.return_value
#         mock_instance.get_bundle_by_id = AsyncMock(return_value={})
#         yield mock
#
# @pytest.fixture
# def callback_service(mock_user_repo, mock_user_order_repo, mock_user_profile_repo,
#                     mock_user_profile_bundle_repo, mock_sync_service, mock_wallet_service,
#                     mock_promotion_service, mock_bundle_service, mock_fcm_service, mock_esim_hub_service):
#     return CallbackService()
#
# class TestCallbackService:
#     @pytest.mark.asyncio
#     async def test_handle_plan_event_callback_consumption_80(self, callback_service, mock_user_profile_repo, mock_user_repo, mock_fcm_service):
#         # Arrange
#         request_data = {
#             "event_type": NotificationCategoryType.CONSUMPTION80.value,
#             "iccid": "iccid_123",
#             "order_id": "order_123"
#         }
#         mock_user_profile_repo.return_value.select.return_value = [{
#             "user_id": "user_123",
#             "esim_hub_order_id": "order_123",
#             "iccid": "iccid_123",
#             "bundle_display_name": "Test Bundle"
#         }]
#         mock_user_repo.return_value.get_by_id.return_value = {
#             "id": "user_123",
#             "email": "test@example.com",
#             "metadata": {"first_name": "Test"}
#         }
#         mock_fcm_service.send_notification_to_user_from_template.return_value = None
#
#         # Act
#         request = MockRequest(request_data)
#         await callback_service.handle_plan_event_callback(request)
#
#         # Assert
#         mock_fcm_service.send_notification_to_user_from_template.assert_called_once()
#
#     @pytest.mark.asyncio
#     async def test_handle_payment_webhook_success(self, callback_service, mock_user_order_repo, mock_bundle_service):
#         # Arrange
#         payment_data = {
#             "type": "payment_intent.succeeded",
#             "data": {
#                 "object": {
#                     "metadata": {
#                         "order_id": "order_123",
#                         "user_id": "user_123",
#                         "bundle_code": "bundle_123",
#                         "env": "DEV"
#                     }
#                 }
#             }
#         }
#         mock_user_order_repo.return_value.get_by_id.return_value = {
#             "id": "order_123",
#             "bundle_data": "{}",
#             "user_id": "user_123"
#         }
#         mock_bundle_service.return_value.buy_bundle = AsyncMock()
#
#         # Act
#         request = MockRequest(payment_data)
#         await callback_service.handle_payment_webhook_fake(request)
#
#         # Assert
#         mock_bundle_service.return_value.buy_bundle.assert_called_once()
#
#     @pytest.mark.asyncio
#     async def test_handle_sync_all_bundles(self, callback_service, mock_sync_service):
#         # Arrange
#         mock_sync_service.return_value.sync_bundles = AsyncMock()
#         mock_sync_service.return_value.update_sync_version = AsyncMock()
#
#         # Act
#         result = await callback_service.handle_sync_all_bundles()
#
#         # Assert
#         assert result.status == "success"
#         # Note: We can't assert the sync calls because they run in a separate thread
#
#     @pytest.mark.asyncio
#     async def test_handle_sync_one_bundle(self, callback_service, mock_sync_service, mock_esim_hub_service):
#         # Arrange
#         bundle_id = "bundle_123"
#         mock_esim_hub_service.return_value.get_bundle_by_id = AsyncMock(return_value={})
#         mock_sync_service.return_value.sync_bundle = AsyncMock()
#         mock_sync_service.return_value.update_sync_version = AsyncMock()
#
#         # Act
#         result = await callback_service.handle_sync_one_bundle(bundle_id)
#
#         # Assert
#         assert result.status == "success"
#         # Note: We can't assert the sync calls because they run in a separate thread
#
#     @pytest.mark.asyncio
#     async def test_handle_user_not_found(self, callback_service, mock_user_repo):
#         # Arrange
#         payment_data = {
#             "type": "payment_intent.succeeded",
#             "data": {
#                 "object": {
#                     "metadata": {
#                         "order_id": "order_123",
#                         "user_id": "invalid_user",
#                         "bundle_code": "bundle_123",
#                         "env": "DEV"
#                     }
#                 }
#             }
#         }
#         mock_user_repo.return_value.get_by_id.return_value = None
#
#         # Act & Assert
#         request = MockRequest(payment_data)
#         with pytest.raises(CustomException) as exc:
#             await callback_service.handle_payment_webhook_fake(request)
#         assert exc.value.code == 404
#         assert "User not found" in str(exc.value)
import pytest
from unittest.mock import MagicMock, patch

from app.models.user import OrderStatusEnum
from app.services.callback_service import CallbackService


@pytest.fixture
def callback_service_bare():
    service = CallbackService.__new__(CallbackService)
    service._CallbackService__user_order_repo = MagicMock()
    service._CallbackService__user_wallet_service = MagicMock()
    service._CallbackService__task_executor = MagicMock()
    return service


def test_wallet_top_up_webhook_refunds_when_daily_limit_exceeded(callback_service_bare):
    service = callback_service_bare
    order = MagicMock(amount=11, payment_intent_code='pi_1', currency='USD')
    service._CallbackService__user_order_repo.get_by_id.return_value = order
    service._CallbackService__user_wallet_service.exceeds_daily_top_up_limit.return_value = True

    metadata = {"user_wallet_id": "wid", "user_id": "u1", "order_id": "o1"}
    with patch('app.services.callback_service.stripe.Refund.create') as mock_refund, \
         patch('app.services.callback_service.fcm_service') as mock_fcm:
        service._CallbackService__handle_wallet_top_up(metadata, "payment_intent.succeeded")

    mock_refund.assert_called_once_with(payment_intent='pi_1')
    service._CallbackService__user_order_repo.update.assert_called_once_with(
        "o1", {"payment_status": OrderStatusEnum.FAILURE})
    service._CallbackService__task_executor.add_task.assert_not_called()
    mock_fcm.send_notification_to_user_from_template.assert_called_once()


def test_wallet_top_up_webhook_credits_when_within_limit(callback_service_bare):
    service = callback_service_bare
    order = MagicMock(amount=10, payment_intent_code='pi_1', currency='USD')
    service._CallbackService__user_order_repo.get_by_id.return_value = order
    service._CallbackService__user_wallet_service.exceeds_daily_top_up_limit.return_value = False

    metadata = {"user_wallet_id": "wid", "user_id": "u1", "order_id": "o1"}
    with patch('app.services.callback_service.stripe.Refund.create') as mock_refund, \
         patch('app.services.callback_service.fcm_service'):
        service._CallbackService__handle_wallet_top_up(metadata, "payment_intent.succeeded")

    mock_refund.assert_not_called()
    service._CallbackService__task_executor.add_task.assert_called_once()
    service._CallbackService__user_order_repo.update.assert_called_once_with(
        "o1", {"payment_status": OrderStatusEnum.SUCCESS})
