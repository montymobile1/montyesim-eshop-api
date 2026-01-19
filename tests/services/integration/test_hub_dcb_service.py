import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from fastapi import HTTPException, Request


# Now import after mocking
from app.services.callback_service import CallbackService, SyncRequest
from app.schemas.callback import ConsumptionLimitRequest
from app.schemas.home import BundleDTO, BundleCategoryDTO, CountryDTO
from app.schemas.response import ResponseHelper
from app.models.user import (
    UserProfileModel,
    UserProfileBundleModel,
    UsersCopyModel,
    OrderStatusEnum,
    UserOrderModel,
    UserOrderType
)
from app.config.db import PaymentTypeEnum


class TestCallbackServiceFixtures:
    """Base fixtures for CallbackService tests"""

    @pytest.fixture
    def mock_repos(self):
        """Mock all repository dependencies"""
        repos = Mock()
        repos.user_repo = Mock()
        repos.user_order_repo = Mock()
        repos.user_profile_repo = Mock()
        repos.user_profile_bundle_repo = Mock()
        return repos

    @pytest.fixture
    def mock_services(self):
        """Mock all service dependencies"""
        services = Mock()
        services.esim_hub_service = Mock()
        services.sync_service = Mock()
        services.user_wallet_service = Mock()
        services.promotion_service = Mock()
        services.bundle_service = Mock()
        services.task_executor = Mock()
        return services

    @pytest.fixture
    def callback_service(self, mock_repos, mock_services):
        """Create CallbackService with mocked dependencies"""
        with patch('app.services.callback_service.esim_hub_service_instance',
                   return_value=mock_services.esim_hub_service), \
                patch('app.services.callback_service.UserRepo', return_value=mock_repos.user_repo), \
                patch('app.services.callback_service.UserOrderRepo', return_value=mock_repos.user_order_repo), \
                patch('app.services.callback_service.UserProfileRepo', return_value=mock_repos.user_profile_repo), \
                patch('app.services.callback_service.UserProfileBundleRepo',
                      return_value=mock_repos.user_profile_bundle_repo), \
                patch('app.services.callback_service.SyncService', return_value=mock_services.sync_service), \
                patch('app.services.callback_service.UserWalletService',
                      return_value=mock_services.user_wallet_service), \
                patch('app.services.callback_service.PromotionService', return_value=mock_services.promotion_service), \
                patch('app.services.callback_service.BundleService', return_value=mock_services.bundle_service), \
                patch('app.services.callback_service.TaskExecutor', return_value=mock_services.task_executor):
            service = CallbackService()
            # Inject mocked dependencies for easier access in tests
            service._CallbackService__user_repo = mock_repos.user_repo
            service._CallbackService__user_order_repo = mock_repos.user_order_repo
            service._CallbackService__user_profile_repo = mock_repos.user_profile_repo
            service._CallbackService__user_profile_bundle_repo = mock_repos.user_profile_bundle_repo
            service._CallbackService__sync_service = mock_services.sync_service
            service._CallbackService__task_executor = mock_services.task_executor
            service._CallbackService__bundle_service = mock_services.bundle_service
            yield service

    @pytest.fixture
    def bundle_category(self):
        """Mock BundleCategoryDTO"""
        return BundleCategoryDTO(
            type="data",
            title="Data Plans",
            code="category-123"
        )

    @pytest.fixture
    def country_dto(self):
        """Mock CountryDTO"""
        return CountryDTO(
            id="country-123",
            alternative_country="LBN",
            country="Lebanon",
            country_code="LB",
            iso3_code="LBN",
            zone_name="Middle East",
            icon="https://example.com/lbn.png",
            operator_list=["Operator1", "Operator2"]
        )

    @pytest.fixture
    def bundle_dto(self, bundle_category, country_dto):
        """Mock BundleDTO"""
        return BundleDTO(
            display_title="5GB Data Plan",
            display_subtitle="Perfect for travelers",
            bundle_code="bundle-123",
            bundle_category=bundle_category,
            bundle_marketing_name="Premium 5GB",
            bundle_name="5GB Plan",
            count_countries=1,
            currency_code="USD",
            gprs_limit=5.0,
            gprs_limit_display="5 GB",
            original_price=10.0,
            price=10.0,
            price_display="10.00 USD",
            unlimited=False,
            validity=30,
            validity_label="Days",
            validity_display="30 Days",
            plan_type="Data only",
            countries=[country_dto],
            icon="https://example.com/bundle.png"
        )

    @pytest.fixture
    def user_profile_bundle(self):
        """Mock UserProfileBundleModel"""
        bundle = Mock(spec=UserProfileBundleModel)
        bundle.id = "bundle-123"
        bundle.user_profile_id = "profile-123"
        bundle.user_order_id = "order-123"
        bundle.iccid = "1234567890"
        bundle.esim_hub_order_id = "esim-order-123"
        bundle.plan_started = True
        bundle.bundle_expired = False
        bundle.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        bundle.bundle_data = {
            "bundle_code": "bundle-123",
            "display_title": "5GB Data Plan",
            "price": 10.0
        }
        bundle.bundle_type = Mock(value="Primary Bundle")
        return bundle

    @pytest.fixture
    def user_profile(self, user_profile_bundle):
        """Mock UserProfileModel"""
        profile = Mock(spec=UserProfileModel)
        profile.id = "profile-123"
        profile.user_id = "user-123"
        profile.shared_user_id = None
        profile.user_order_id = "order-123"
        profile.iccid = "1234567890"
        profile.validity = datetime(2024, 12, 31, tzinfo=timezone.utc)
        profile.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        profile.label = "My eSIM"
        profile.smdp_address = "smdp.example.com"
        profile.activation_code = "ABC123"
        profile.allow_topup = True
        profile.esim_hub_order_id = "esim-order-123"
        profile.searched_countries = None
        profile.bundles = [user_profile_bundle]
        return profile

    @pytest.fixture
    def user_model(self):
        """Mock UsersCopyModel"""
        user = Mock(spec=UsersCopyModel)
        user.id = "user-123"
        user.email = "test@example.com"
        user.metadata = {
            "first_name": "John",
            "last_name": "Doe",
            "email": "test@example.com",
            "language": "en",
            "display_email": "test@example.com"
        }
        return user

    @pytest.fixture
    def consumption_limit_request(self):
        """Mock ConsumptionLimitRequest"""
        return ConsumptionLimitRequest(
            order_id="esim-order-123",
            iccid="1234567890",
            event_type="limit_80",
            event_date="2025-01-15T10:00:00Z"
        )

    @pytest.fixture
    def mock_request(self, consumption_limit_request):
        """Mock FastAPI Request object"""
        request = Mock(spec=Request)

        # Create proper async mock methods
        async def mock_json():
            return consumption_limit_request.model_dump()

        async def mock_body():
            return b'{"test": "data"}'

        request.json = mock_json
        request.body = mock_body
        request.headers = {"stripe-signature": "test_signature"}
        return request


class TestHandlePlanEventCallback(TestCallbackServiceFixtures):
    """Test cases for handle_plan_event_callback method"""

    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_80_consumption(
            self,
            callback_service,
            mock_request,
            user_profile_bundle,
            user_profile,
            user_model,
            mock_repos
    ):
        """Test handling 80% consumption event"""
        # Setup mocks
        mock_repos.user_profile_bundle_repo.get_first_by.return_value = user_profile_bundle
        mock_repos.user_profile_repo.get_by_id.return_value = user_profile
        mock_repos.user_repo.get_by_id.return_value = user_model

        with patch('app.services.callback_service.DtoMapper.to_order_notification_model') as mock_mapper, \
                patch('app.services.callback_service.fcm_service') as mock_fcm, \
                patch('app.services.callback_service.send_consumption_80_bundle_notification') as mock_notification, \
                patch.object(callback_service, '_CallbackService__send_email_80_consumption',
                             new_callable=AsyncMock) as mock_email:
            mock_order_model = Mock()
            mock_order_model.user_id = "user-123"
            mock_order_model.user_display_name = "John Doe"
            mock_order_model.bundle_display_name = "5GB Data Plan"
            mock_mapper.return_value = mock_order_model
            mock_notification.return_value = {"title": "80% Used", "body": "You've used 80% of your data"}

            # Execute
            await callback_service.handle_plan_event_callback(mock_request)

            # Verify
            mock_repos.user_profile_bundle_repo.get_first_by.assert_called_once_with(
                where={"iccid": "1234567890", "esim_hub_order_id": "esim-order-123"}
            )
            mock_repos.user_profile_repo.get_by_id.assert_called_once_with(record_id="profile-123")
            mock_email.assert_called_once()
            mock_fcm.send_notification_to_user_from_template.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_100_consumption(
            self,
            callback_service,
            mock_request,
            user_profile_bundle,
            user_profile,
            user_model,
            mock_repos
    ):
        """Test handling 100% consumption event"""

        # Modify request for 100% consumption
        async def mock_json_100():
            return {
                "order_id": "esim-order-123",
                "iccid": "1234567890",
                "event_type": "limit_100",
                "event_date": "2025-01-15T10:00:00Z"
            }

        mock_request.json = mock_json_100

        # Setup mocks
        mock_repos.user_profile_bundle_repo.get_first_by.return_value = user_profile_bundle
        mock_repos.user_profile_repo.get_by_id.return_value = user_profile
        mock_repos.user_repo.get_by_id.return_value = user_model

        with patch('app.services.callback_service.DtoMapper.to_order_notification_model') as mock_mapper, \
                patch('app.services.callback_service.fcm_service') as mock_fcm, \
                patch('app.services.callback_service.send_consumption_100_bundle_notification') as mock_notification, \
                patch.object(callback_service, '_CallbackService__send_email_100_consumption',
                             new_callable=AsyncMock) as mock_email, \
                patch.object(callback_service, '_CallbackService__update_bundle_expired') as mock_update:
            mock_order_model = Mock()
            mock_order_model.user_id = "user-123"
            mock_order_model.user_display_name = "John Doe"
            mock_order_model.bundle_display_name = "5GB Data Plan"
            mock_mapper.return_value = mock_order_model
            mock_notification.return_value = {"title": "Data Exhausted", "body": "Your data has been used up"}

            # Execute
            await callback_service.handle_plan_event_callback(mock_request)

            # Verify
            mock_email.assert_called_once()
            mock_update.assert_called_once_with(
                iccid="1234567890",
                esim_hub_order_id="esim-order-123",
                bundle_expired=True
            )

    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_plan_started(
            self,
            callback_service,
            mock_request,
            user_profile_bundle,
            user_profile,
            user_model,
            mock_repos
    ):
        """Test handling plan started event"""

        # Modify request for plan started
        async def mock_json_started():
            return {
                "order_id": "esim-order-123",
                "iccid": "1234567890",
                "event_type": "StartBundle",
                "event_date": "2025-01-15T10:00:00Z"
            }

        mock_request.json = mock_json_started

        # Setup mocks
        mock_repos.user_profile_bundle_repo.get_first_by.return_value = user_profile_bundle
        mock_repos.user_profile_repo.get_by_id.return_value = user_profile
        mock_repos.user_repo.get_by_id.return_value = user_model

        with patch('app.services.callback_service.DtoMapper.to_order_notification_model') as mock_mapper, \
                patch('app.services.callback_service.fcm_service') as mock_fcm, \
                patch('app.services.callback_service.send_plan_started_notification') as mock_notification, \
                patch.object(callback_service, '_CallbackService__update_bundle_plan_started') as mock_update, \
                patch('app.services.callback_service.parse_iso_datetime') as mock_parse:
            mock_order_model = Mock()
            mock_order_model.user_id = "user-123"
            mock_order_model.bundle_display_name = "5GB Data Plan"
            mock_order_model.validity = "2025-02-15T10:00:00Z"
            mock_mapper.return_value = mock_order_model

            mock_datetime = datetime(2025, 2, 15, tzinfo=timezone.utc)
            mock_parse.return_value = mock_datetime
            mock_notification.return_value = {"title": "Plan Started", "body": "Your plan is now active"}

            # Execute
            await callback_service.handle_plan_event_callback(mock_request)

            # Verify
            mock_update.assert_called_once_with(
                iccid="1234567890",
                esim_hub_order_id="esim-order-123",
                plan_started=True
            )
            mock_notification.assert_called_once_with(
                bundle_name="5GB Data Plan",
                validity_date="2025-02-15"
            )

    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_no_bundle_found(
            self,
            callback_service,
            mock_request,
            mock_repos
    ):
        """Test when user profile bundle is not found"""
        # Setup mock to return None
        mock_repos.user_profile_bundle_repo.get_first_by.return_value = None

        # Execute - should return early without raising exception
        await callback_service.handle_plan_event_callback(mock_request)

        # Verify early return
        mock_repos.user_profile_repo.get_by_id.assert_not_called()


    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_unsupported_event_type(
            self,
            callback_service,
            mock_request,
            user_profile_bundle,
            user_profile,
            user_model,
            mock_repos
    ):
        """Test handling unsupported event type"""

        # Modify request with unsupported event
        async def mock_json_unsupported():
            return {
                "order_id": "esim-order-123",
                "iccid": "1234567890",
                "event_type": "UNKNOWN_EVENT",
                "event_date": "2025-01-15T10:00:00Z"
            }

        mock_request.json = mock_json_unsupported

        # Setup mocks
        mock_repos.user_profile_bundle_repo.get_first_by.return_value = user_profile_bundle
        mock_repos.user_profile_repo.get_by_id.return_value = user_profile
        mock_repos.user_repo.get_by_id.return_value = user_model

        with patch('app.services.callback_service.DtoMapper.to_order_notification_model') as mock_mapper, \
                patch('app.services.callback_service.fcm_service') as mock_fcm:
            mock_order_model = Mock()
            mock_order_model.user_id = "user-123"
            mock_mapper.return_value = mock_order_model

            # Execute
            await callback_service.handle_plan_event_callback(mock_request)

            # Verify no notification was sent
            mock_fcm.send_notification_to_user_from_template.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_exception_handling(
            self,
            callback_service,
            mock_request,
            mock_repos
    ):
        """Test exception handling in callback"""
        # Setup mock to raise exception
        mock_repos.user_profile_bundle_repo.get_first_by.side_effect = Exception("Database error")

        # Execute and verify exception is raised
        with pytest.raises(HTTPException) as exc_info:
            await callback_service.handle_plan_event_callback(mock_request)

        assert exc_info.value.status_code == 500
        assert "Database error" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_all_80_event_variants(
            self,
            callback_service,
            mock_request,
            user_profile_bundle,
            user_profile,
            user_model,
            mock_repos
    ):
        """Test all variants of 80% consumption event types"""
        event_variants = ["limit_80", "PLAN-80", "Eighty"]

        for event_type in event_variants:
            # Reset mocks
            mock_repos.reset_mock()
            mock_repos.user_profile_bundle_repo.get_first_by.return_value = user_profile_bundle
            mock_repos.user_profile_repo.get_by_id.return_value = user_profile
            mock_repos.user_repo.get_by_id.return_value = user_model

            # Modify request
            async def mock_json_variant():
                return {
                    "order_id": "esim-order-123",
                    "iccid": "1234567890",
                    "event_type": event_type,
                    "event_date": "2025-01-15T10:00:00Z"
                }

            mock_request.json = mock_json_variant

            with patch('app.services.callback_service.DtoMapper.to_order_notification_model') as mock_mapper, \
                    patch('app.services.callback_service.send_consumption_80_bundle_notification') as mock_notification, \
                    patch.object(callback_service, '_CallbackService__send_email_80_consumption',
                                 new_callable=AsyncMock) as mock_email:
                mock_order_model = Mock()
                mock_order_model.user_id = "user-123"
                mock_order_model.bundle_display_name = "5GB Data Plan"
                mock_mapper.return_value = mock_order_model
                mock_notification.return_value = {"title": "80%", "body": "test"}

                # Execute
                await callback_service.handle_plan_event_callback(mock_request)

                # Verify
                mock_notification.assert_called_once()
                mock_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_plan_event_callback_all_100_event_variants(
            self,
            callback_service,
            mock_request,
            user_profile_bundle,
            user_profile,
            user_model,
            mock_repos
    ):
        """Test all variants of 100% consumption event types"""
        event_variants = ["limit_100", "PLAN-100", "DATA_LIMIT", "PREPAID_PLAN_COMPLETION"]

        for event_type in event_variants:
            # Reset mocks
            mock_repos.reset_mock()
            mock_repos.user_profile_bundle_repo.get_first_by.return_value = user_profile_bundle
            mock_repos.user_profile_repo.get_by_id.return_value = user_profile
            mock_repos.user_repo.get_by_id.return_value = user_model

            # Modify request
            async def mock_json_variant():
                return {
                    "order_id": "esim-order-123",
                    "iccid": "1234567890",
                    "event_type": event_type,
                    "event_date": "2025-01-15T10:00:00Z"
                }

            mock_request.json = mock_json_variant

            with patch('app.services.callback_service.DtoMapper.to_order_notification_model') as mock_mapper, \
                    patch(
                        'app.services.callback_service.send_consumption_100_bundle_notification') as mock_notification, \
                    patch.object(callback_service, '_CallbackService__send_email_100_consumption',
                                 new_callable=AsyncMock) as mock_email, \
                    patch.object(callback_service, '_CallbackService__update_bundle_expired') as mock_update:
                mock_order_model = Mock()
                mock_order_model.user_id = "user-123"
                mock_order_model.bundle_display_name = "5GB Data Plan"
                mock_mapper.return_value = mock_order_model
                mock_notification.return_value = {"title": "100%", "body": "test"}

                # Execute
                await callback_service.handle_plan_event_callback(mock_request)

                # Verify
                mock_notification.assert_called_once()
                mock_update.assert_called_once()


class TestHandlePaymentWebhook(TestCallbackServiceFixtures):
    """Test cases for handle_payment_webhook method"""

    @pytest.mark.asyncio
    async def test_handle_payment_webhook_success(
            self,
            callback_service,
            mock_request
    ):
        """Test successful webhook processing"""
        with patch('app.services.callback_service.stripe.Webhook.construct_event') as mock_construct, \
                patch.object(callback_service._CallbackService__task_executor, 'add_task') as mock_add_task:
            mock_event = {"type": "payment_intent.succeeded", "data": {"object": {}}}
            mock_construct.return_value = mock_event

            # Execute
            result = await callback_service.handle_payment_webhook(mock_request)

            # Verify
            assert result.status == "success"
            mock_add_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_payment_webhook_invalid_payload(
            self,
            callback_service,
            mock_request
    ):
        """Test webhook with invalid payload"""
        with patch('app.services.callback_service.stripe.Webhook.construct_event') as mock_construct:
            mock_construct.side_effect = ValueError("Invalid payload")

            # Execute and verify exception
            with pytest.raises(HTTPException) as exc_info:
                await callback_service.handle_payment_webhook(mock_request)

            assert exc_info.value.status_code == 400
            assert "Invalid payload" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_handle_payment_webhook_invalid_signature(
            self,
            callback_service,
            mock_request
    ):
        """Test webhook with invalid signature"""
        with patch('app.services.callback_service.stripe.Webhook.construct_event') as mock_construct, \
                patch('app.services.callback_service.stripe.error.SignatureVerificationError', Exception):
            from stripe import error
            mock_construct.side_effect = error.SignatureVerificationError("Invalid signature", "sig")

            # Execute and verify exception
            with pytest.raises(HTTPException) as exc_info:
                await callback_service.handle_payment_webhook(mock_request)

            assert exc_info.value.status_code == 400
            assert "Invalid signature" in exc_info.value.detail


class TestSyncOperations(TestCallbackServiceFixtures):
    """Test cases for sync operations"""

    def test_handle_sync_all_bundles(self, callback_service):
        """Test triggering full bundle sync"""
        result = callback_service.handle_sync_all_bundles(page_index=1)

        assert result.status == "success"
        callback_service._CallbackService__task_executor.add_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_sync_one_bundle_insert(self, callback_service, mock_request):
        """Test syncing a single bundle with insert operation"""

        async def mock_json_insert():
            return {
                "operation": "insert",
                "bundle_id": "bundle-123",
                "reseller_id": "reseller-456"
            }

        mock_request.json = mock_json_insert

        result = await callback_service.handle_sync_one_bundle(mock_request)

        assert result.status == "success"
        callback_service._CallbackService__task_executor.add_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_sync_one_bundle_update(self, callback_service, mock_request):
        """Test syncing a single bundle with update operation"""

        async def mock_json_update():
            return {
                "operation": "update",
                "bundle_id": "bundle-123",
                "reseller_id": None
            }

        mock_request.json = mock_json_update

        result = await callback_service.handle_sync_one_bundle(mock_request)

        assert result.status == "success"
        callback_service._CallbackService__task_executor.add_task.assert_called_once()


class TestExchangeRateUpdate(TestCallbackServiceFixtures):
    """Test cases for exchange rate updates"""


    @pytest.mark.asyncio
    async def test_handle_exchange_rate_update_wrong_reseller(self, callback_service, mock_request):
        """Test exchange rate update for different reseller"""

        async def mock_json_wrong_reseller():
            return {
                "systemCurrencyCode": "USD",
                "currencyCode": "EUR",
                "resellerId": "different-reseller",
                "newRate": "1.2"
            }

        mock_request.json = mock_json_wrong_reseller

        with patch('app.services.callback_service.get_config', return_value='test-reseller'):
            result = await callback_service.handle_exchange_rate_update(mock_request)

            assert result.status == "success"
            callback_service._CallbackService__sync_service.update_sync_version.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_exchange_rate_update_non_usd_system_currency(self, callback_service, mock_request):
        """Test exchange rate update with non-USD system currency"""

        async def mock_json_non_usd():
            return {
                "systemCurrencyCode": "EUR",
                "currencyCode": "GBP",
                "resellerId": None,
                "newRate": "0.9"
            }

        mock_request.json = mock_json_non_usd

        result = await callback_service.handle_exchange_rate_update(mock_request)

        assert result.status == "success"
        callback_service._CallbackService__sync_service.update_sync_version.assert_not_called()


class TestEmailNotifications(TestCallbackServiceFixtures):
    """Test cases for email notifications"""

    @pytest.mark.asyncio
    async def test_send_email_80_consumption(self, callback_service, user_model):
        """Test sending 80% consumption email"""
        with patch('app.services.callback_service.get_email_template') as mock_template, \
                patch('app.services.callback_service.send_email') as mock_send_email, \
                patch('app.services.callback_service.get_config', return_value='https://example.com'), \
                patch.dict('os.environ', {'WHATSAPP_NUMBER': '+1234567890'}):
            mock_template_obj = Mock()
            mock_template_obj.render.return_value = "<html>Test</html>"
            mock_template.return_value = mock_template_obj

            await callback_service._CallbackService__send_email_80_consumption(
                user=user_model,
                bundle_name="5GB Plan",
                iccid="1234567890"
            )

            mock_send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_email_100_consumption(self, callback_service, user_model):
        """Test sending 100% consumption email"""
        with patch('app.services.callback_service.get_email_template') as mock_template, \
                patch('app.services.callback_service.send_email') as mock_send_email, \
                patch('app.services.callback_service.get_config', return_value='https://example.com'), \
                patch.dict('os.environ', {'WHATSAPP_NUMBER': '+1234567890'}):
            mock_template_obj = Mock()
            mock_template_obj.render.return_value = "<html>Test</html>"
            mock_template.return_value = mock_template_obj

            await callback_service._CallbackService__send_email_100_consumption(
                user=user_model,
                bundle_name="5GB Plan",
                iccid="1234567890"
            )

            mock_send_email.assert_called_once()


class TestBundleStatusUpdates(TestCallbackServiceFixtures):
    """Test cases for bundle status updates"""

    def test_update_bundle_expired(self, callback_service, mock_repos):
        """Test updating bundle expired status"""
        callback_service._CallbackService__update_bundle_expired(
            iccid="1234567890",
            esim_hub_order_id="esim-order-123",
            bundle_expired=True
        )

        mock_repos.user_profile_bundle_repo.update_by.assert_called_once_with(
            where={"esim_hub_order_id": "esim-order-123", "iccid": "1234567890"},
            data={"bundle_expired": True}
        )

    def test_update_bundle_plan_started(self, callback_service, mock_repos):
        """Test updating bundle plan started status"""
        callback_service._CallbackService__update_bundle_plan_started(
            iccid="1234567890",
            esim_hub_order_id="esim-order-123",
            plan_started=True
        )

        mock_repos.user_profile_bundle_repo.update_by.assert_called_once_with(
            where={"esim_hub_order_id": "esim-order-123", "iccid": "1234567890"},
            data={"plan_started": True}
        )


class TestConsumptionLimitRequest(TestCallbackServiceFixtures):
    """Test cases for ConsumptionLimitRequest schema"""

    def test_consumption_limit_request_valid(self):
        """Test valid ConsumptionLimitRequest creation"""
        request = ConsumptionLimitRequest(
            order_id="order-123",
            iccid="1234567890",
            event_type="limit_80",
            event_date="2025-01-15T10:00:00Z"
        )

        assert request.order_id == "order-123"
        assert request.iccid == "1234567890"
        assert request.event_type == "limit_80"
        assert request.event_date == "2025-01-15T10:00:00Z"

    def test_consumption_limit_request_optional_fields(self):
        """Test ConsumptionLimitRequest with optional fields"""
        request = ConsumptionLimitRequest(
            order_id="order-123",
            iccid="1234567890",
            event_type=None,
            event_date=None
        )

        assert request.order_id == "order-123"
        assert request.iccid == "1234567890"
        assert request.event_type is None
        assert request.event_date is None

    def test_consumption_limit_request_from_dict(self):
        """Test creating ConsumptionLimitRequest from dictionary"""
        data = {
            "order_id": "order-123",
            "iccid": "1234567890",
            "event_type": "StartBundle",
            "event_date": "2025-01-15T10:00:00Z"
        }

        request = ConsumptionLimitRequest.model_validate(data)

        assert request.order_id == "order-123"
        assert request.iccid == "1234567890"
        assert request.event_type == "StartBundle"


