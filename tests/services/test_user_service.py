"""
Comprehensive test suite for UserBundleService

This test suite covers all major methods and edge cases for the UserBundleService class,
including payment flows, order management, bundle operations, and error handling.
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dis import disco
from typing import List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import Request

from app.config.constants import ErrorMessages, PaymentStatusEnum, UserWalletTransactionSource
from app.config.db import PaymentTypeEnum, OrderStatusEnum
from app.exceptions import BadRequestException, CustomException
from app.models.user import (
    UserModel,
    UserOrderModel,
    UserOrderType,
    UserProfileModel,
    UserProfileBundleModel,
    UserWalletModel,
    UsersCopyModel,
)
from app.schemas.bundle import (
    AssignRequest,
    AssignTopUpRequest,
    ConsumptionResponse,
    EsimBundleResponse,
    PaymentIntentResponse,
    UpdateBundleLabelRequest,
    VerifyOtpRequestDto,
    RelatedSearchRequestDto,
    CountryRequestDto
)
from app.schemas.response import Response, ResponseHelper
from app.config.db import UserBundleType
from app.schemas.home import BundleDTO, BundleCategoryDTO, CountryDTO
from app.services.user_service import UserBundleService


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mock_env_vars():
    """Mock all environment variables"""
    with patch.dict(os.environ, {
        'STRIPE_PUBLIC_KEY': 'pk_test_mock',
        'MERCHANT_ID': 'merchant_test_mock',
        'MERCHANT_DISPLAY_NAME': 'Test Merchant',
        'DEFAULT_CURRENCY': 'USD',
        'ENVIRONMENT': 'TEST',
        'ESIM_HUB_BASE_URL': 'https://test.esimhub.com',
        'ESIM_HUB_TENANT_KEY': 'test_tenant_key',
        'MAX_KEEPALIVE_CONNECTIONS': '100',
        'MAX_CONNECTIONS': '200',
    }):
        yield


@pytest.fixture
def mock_user():
    """Create a mock user for testing"""
    return UserModel(
        id="user-123",
        email="test@example.com",
        token="13",
        msisdn="+1234567890",
        anonymous_user_id="anon-123",
        is_verified=True,
    )


@pytest.fixture
def mock_bundle():
    """Create a mock bundle DTO"""
    return BundleDTO(
        bundle_code="BUNDLE-001",
        display_title="Test Bundle",
        display_subtitle="Test Subtitle",
        bundle_marketing_name="Test Marketing Name",
        bundle_name="Test Bundle",
        count_countries=2,
        currency_code="US",
        gprs_limit_display="10",
        bundle_category= BundleCategoryDTO(
            type="GLOBAL",
            title="Data Plans",
            code="category-123"
        ),
        original_price=10.00,
        is_active=True,
        is_stockable=True,
        bundle_info_code="INFO-001",
        label="Test Bundle",
        data_amount="1GB",
        validity_days=30,
        price=1.0,
        price_display="1",
        unlimited=False,
        validity=7,
        validity_display="7 days",
        countries=[ CountryDTO(
            id="country-123",
            alternative_country="LBN",
            country="Lebanon",
            country_code="LB",
            iso3_code="LBN",
            zone_name="Middle East",
            icon="https://example.com/lbn.png",
            operator_list=["Operator1", "Operator2"]
        )
]

    )


@pytest.fixture
def mock_assign_request():
    """Create a mock assign request"""
    return AssignRequest(
        bundle_code="BUNDLE-001",
        promo_code=None,
        affiliate_code=None,
        payment_type=PaymentTypeEnum.CARD,
        related_search=RelatedSearchRequestDto(countries=[CountryRequestDto(country_name="Lebanon",iso3_code="LBN")]),
    )


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request"""
    request = Mock(spec=Request)
    request.client = Mock()
    request.client.host = "192.168.1.1"
    return request


@pytest.fixture
def mock_config_repo():
    """Mock the ConfigRepo used by get_config"""
    with patch('app.repo.config_repo.ConfigRepo') as mock_repo_class:
        mock_repo_instance = Mock()
        mock_repo_class.return_value = mock_repo_instance

        # Mock get_first_by to return None (will use env vars)
        mock_repo_instance.get_first_by.return_value = None
        mock_repo_instance.create.return_value = None

        yield mock_repo_instance


@pytest.fixture
def user_service(mock_env_vars, mock_config_repo):
    """Create UserBundleService with fully mocked dependencies"""

    # Mock the service factory functions BEFORE importing/instantiating
    with patch('app.services.user_service.esim_hub_service_instance') as mock_esim_factory, \
            patch('app.services.user_service.dcb_service_instance') as mock_dcb_factory, \
            patch('app.services.user_service.supabase_client') as mock_supabase_factory, \
            patch('app.services.user_service.get_config') as mock_get_config:
        # Configure get_config mock
        def get_config_side_effect(key, default=None):
            config_values = {
                'ESIM_HUB_API_KEY': 'test_api_key',
                'OTP_EXPIRATION_TIME': '5',
            }
            if isinstance(key, str):
                return config_values.get(key, default or os.getenv(key, default))
            return config_values.get(key.value if hasattr(key, 'value') else str(key), default)

        mock_get_config.side_effect = get_config_side_effect

        # Create mock services
        mock_esim_service = AsyncMock()
        mock_dcb_service = AsyncMock()
        mock_supabase = Mock()

        mock_esim_factory.return_value = mock_esim_service
        mock_dcb_factory.return_value = mock_dcb_service
        mock_supabase_factory.return_value = mock_supabase

        # Mock all repository classes
        with patch('app.services.user_service.NotificationRepo') as MockNotificationRepo, \
                patch('app.services.user_service.UserOrderRepo') as MockUserOrderRepo, \
                patch('app.services.user_service.UserProfileRepo') as MockUserProfileRepo, \
                patch('app.services.user_service.UserProfileBundleRepo') as MockUserProfileBundleRepo, \
                patch('app.services.user_service.UserWalletService') as MockUserWalletService, \
                patch('app.services.user_service.PromotionService') as MockPromotionService, \
                patch('app.services.user_service.BundleService') as MockBundleService, \
                patch('app.services.user_service.CurrencyService') as MockCurrencyService, \
                patch('app.services.user_service.UserRepo') as MockUserRepo, \
                patch('app.services.user_service.TaskExecutor') as MockTaskExecutor, \
                patch('app.services.user_service.BundleTranslationRepo') as MockBundleTranslationRepo:
            # Create mock instances
            mock_notification_repo = Mock()
            mock_user_order_repo = Mock()
            mock_user_profile_repo = Mock()
            mock_user_profile_bundle_repo = Mock()
            mock_user_wallet_service = Mock()
            mock_promotion_service = AsyncMock()
            mock_bundle_service = Mock()
            mock_currency_service = Mock()
            mock_user_repo = Mock()
            mock_task_executor = Mock()
            mock_bundle_translation_repo = Mock()

            # Configure repository constructors to return mocks
            MockNotificationRepo.return_value = mock_notification_repo
            MockUserOrderRepo.return_value = mock_user_order_repo
            MockUserProfileRepo.return_value = mock_user_profile_repo
            MockUserProfileBundleRepo.return_value = mock_user_profile_bundle_repo
            MockUserWalletService.return_value = mock_user_wallet_service
            MockPromotionService.return_value = mock_promotion_service
            MockBundleService.return_value = mock_bundle_service
            MockCurrencyService.return_value = mock_currency_service
            MockUserRepo.return_value = mock_user_repo
            MockTaskExecutor.return_value = mock_task_executor
            MockBundleTranslationRepo.return_value = mock_bundle_translation_repo

            # Now create the service
            service = UserBundleService()

            # Store references to mocks for test access
            service._test_mocks = {
                'esim_hub_service': mock_esim_service,
                'notification_repo': mock_notification_repo,
                'user_order_repo': mock_user_order_repo,
                'user_profile_repo': mock_user_profile_repo,
                'user_profile_bundle_repo': mock_user_profile_bundle_repo,
                'user_wallet_service': mock_user_wallet_service,
                'promotion_service': mock_promotion_service,
                'bundle_service': mock_bundle_service,
                'dcb_service': mock_dcb_service,
                'currency_service': mock_currency_service,
                'user_repo': mock_user_repo,
                'task_executor': mock_task_executor,
                'bundle_translation_repo': mock_bundle_translation_repo,
                'supabase_client': mock_supabase,
            }

            yield service


# ============================================================================
# ASSIGN METHOD TESTS
# ============================================================================


class TestAssign:
    """Tests for the assign method (bundle purchase flow)"""

    @pytest.mark.asyncio
    async def test_assign_success_with_card_payment(
            self, user_service, mock_user, mock_bundle, mock_assign_request, mock_request
    ):
        """Test successful bundle assignment with card payment"""

        # Get references to mocked services
        mocks = user_service._test_mocks

        # Setup mock responses
        mocks['esim_hub_service'].get_bundle_by_id.return_value = mock_bundle

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.ASSIGN,
            amount=1000,
            modified_amount=1000,
            currency="USD",
            bundle_data=mock_bundle.model_dump_json(),
            payment_type=PaymentTypeEnum.CARD,
            tax_amount=0,
            created_at="2025-01-01T00:00:00Z",
        )
        mocks['user_order_repo'].create.return_value = mock_order

        # Mock currency service - BOTH methods
        mocks['currency_service'].get_currency_rate.return_value = 1.0
        mocks['currency_service'].convert.return_value = 1.0  # Add this!

        # Mock Stripe operations
        with patch("app.services.user_service.create_payment_intent") as mock_create_payment, \
                patch("app.services.user_service.create_payment_ephemeral") as mock_ephemeral:
            # Create mock payment intent
            mock_payment_intent = Mock()
            mock_payment_intent.id = "pi_123"
            mock_payment_intent.client_secret = "secret_123"
            mock_payment_intent.customer = "cus_123"
            mock_payment_intent.livemode = True
            mock_payment_intent.amount = 1000

            # Create mock tax
            mock_tax = Mock()
            mock_tax.tax_amount_exclusive = 100

            mock_create_payment.return_value = (mock_payment_intent, mock_tax)

            # Create mock ephemeral key
            mock_ephemeral_key = Mock()
            mock_ephemeral_key.secret = "eph_secret_123"
            mock_ephemeral.return_value = mock_ephemeral_key

            # Execute the method
            result = await user_service.assign(
                user=mock_user,
                device_id="device-123",
                assign_request=mock_assign_request,
                x_currency="USD",
                locale="en",
                request=mock_request,
            )

        # Assertions
        assert result.status == "success"
        assert isinstance(result.data, PaymentIntentResponse)
        assert result.data.order_id == "order-123"
        assert result.data.payment_intent_client_secret == "secret_123"

        # Verify mocks were called correctly
        mocks['esim_hub_service'].get_bundle_by_id.assert_called_once_with(
            bundle_id=mock_assign_request.bundle_code
        )
        mocks['user_order_repo'].create.assert_called_once()
        mock_create_payment.assert_called_once()




    @pytest.mark.asyncio
    async def test_assign_bundle_not_available(
            self, user_service, mock_user, mock_assign_request, mock_request
    ):
        """Test assignment when bundle is not available"""
        user_service._UserBundleService__esim_hub_service.get_bundle_by_id.return_value = None

        with pytest.raises(CustomException) as exc_info:
            await user_service.assign(
                user=mock_user,
                device_id="device-123",
                assign_request=mock_assign_request,
                x_currency="USD",
                locale="en",
                request=mock_request,
            )

        assert exc_info.value.code == 400
        assert ErrorMessages.BUNDLE_NOT_AVAILABLE in exc_info.value.name

    @pytest.mark.asyncio
    async def test_assign_with_promo_code(
            self, user_service, mock_user, mock_bundle, mock_assign_request, mock_request
    ):
        """Test assignment with valid promo code"""
        mocks = user_service._test_mocks

        # Add promo code to request
        mock_assign_request.promo_code = "PROMO20"

        # Setup mocks
        mocks['esim_hub_service'].get_bundle_by_id.return_value = mock_bundle

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.ASSIGN,
            amount=1000,
            modified_amount=800,  # 20% discount
            currency="USD",
            bundle_data=mock_bundle.model_dump_json(),
            payment_type=PaymentTypeEnum.CARD,
            tax_amount=0,
            created_at="2025-01-01T00:00:00Z",
        )
        mocks['user_order_repo'].create.return_value = mock_order

        # Mock promotion service
        discounted_bundle = mock_bundle
        discounted_bundle.original_price = 8.00

        mock_validation_response = Mock()
        mock_validation_response.bundle = discounted_bundle
        mock_validation_response.rule_id = "rule-123"
        mock_validation_response.message = "Promo code applied"

        mocks['promotion_service'].is_referral_code.return_value = False
        mocks['promotion_service'].validate_promo_code.return_value = mock_validation_response

        # Mock user_profile_repo.list to return empty list (user has no previous profiles)
        # This is checked in __check_if_user_eligible_for_referral for non-referral promo codes
        mocks['user_profile_repo'].list.return_value = []

        # Mock user_repo.get_by_id to return UsersCopyModel with metadata
        # This is also checked in __check_if_user_eligible_for_referral
        mock_user_copy = UsersCopyModel(
            id=mock_user.id,
            email=mock_user.email,
            metadata={"referral_code": "USER_OWN_CODE", "language": "en"}  # Different from promo code
        )
        mocks['user_repo'].get_by_id.return_value = mock_user_copy

        # Mock currency service - both methods
        mocks['currency_service'].get_currency_rate.return_value = 1.0
        mocks['currency_service'].convert.return_value = 0.0  # No tax in this test

        # Mock Stripe operations
        with patch("app.services.user_service.create_payment_intent") as mock_create_payment, \
                patch("app.services.user_service.create_payment_ephemeral") as mock_ephemeral:
            mock_payment_intent = Mock()
            mock_payment_intent.id = "pi_promo_123"
            mock_payment_intent.client_secret = "secret_promo_123"
            mock_payment_intent.customer = "cus_123"
            mock_payment_intent.livemode = True
            mock_payment_intent.amount = 800

            mock_tax = Mock()
            mock_tax.tax_amount_exclusive = 0

            mock_create_payment.return_value = (mock_payment_intent, mock_tax)

            mock_ephemeral_key = Mock()
            mock_ephemeral_key.secret = "eph_secret_promo_123"
            mock_ephemeral.return_value = mock_ephemeral_key

            result = await user_service.assign(
                user=mock_user,
                device_id="device-123",
                assign_request=mock_assign_request,
                x_currency="USD",
                locale="en",
                request=mock_request,
            )

        assert result.status == "success"
        mocks['promotion_service'].validate_promo_code.assert_called_once()
        mocks['task_executor'].add_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_assign_free_bundle_with_promo(
            self, user_service, mock_user, mock_bundle, mock_assign_request, mock_request
    ):
        """Test assignment when promo code makes bundle free (100% discount)"""
        mock_assign_request.promo_code = "FREE100"
        mocks = user_service._test_mocks

        mocks['esim_hub_service'].get_bundle_by_id.return_value = mock_bundle

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.ASSIGN,
            amount=1000,
            modified_amount=0,  # Free with promo
            currency="USD",
            bundle_data=mock_bundle.model_dump_json(),
            payment_type=PaymentTypeEnum.CARD,
        )
        mocks['user_order_repo'].create.return_value = mock_order

        # Mock promotion validation
        free_bundle = mock_bundle.model_copy()
        free_bundle.original_price = 0.00

        mock_validation_response = Mock()
        mock_validation_response.bundle = free_bundle
        mock_validation_response.rule_id = "rule-free"
        mock_validation_response.message = "Free bundle promo"

        mocks['promotion_service'].is_referral_code.return_value = False
        mocks['promotion_service'].validate_promo_code.return_value = mock_validation_response
        mocks['user_profile_repo'].list.return_value = []

        mock_user_copy = UsersCopyModel(
            id=mock_user.id,
            email=mock_user.email,
            metadata={"referral_code": "USER_OWN_CODE", "language": "en"}
        )
        mocks['user_repo'].get_by_id.return_value = mock_user_copy

        # Mock currency service
        mocks['currency_service'].get_currency_rate.return_value = 1.0
        mocks['currency_service'].convert.return_value = 0.0

        # CRITICAL FIX: Mock bundle_service, not esim_hub_service
        mocks['bundle_service'].buy_bundle = AsyncMock(
            return_value=ResponseHelper.success_data_response(True, 0)
        )

        result = await user_service.assign(
            user=mock_user,
            device_id="device-123",
            assign_request=mock_assign_request,
            x_currency="USD",
            locale="en",
            request=mock_request,
        )

        assert result.status == "success"
        assert result.data.payment_status == PaymentStatusEnum.COMPLETED

        # Verify buy_bundle was called with correct parameters
        mocks['bundle_service'].buy_bundle.assert_called_once_with(
            user_order=mock_order,
            bundle=free_bundle,  # Should be the modified free bundle
            user_id=mock_user.id,
            payment_status=OrderStatusEnum.SUCCESS,
            rule_id="rule-free",
            payment_type=PaymentTypeEnum.CARD
        )


    @pytest.mark.asyncio
    async def test_assign_insufficient_wallet_balance(
            self, user_service, mock_user, mock_bundle, mock_request
    ):
        """Test assignment fails when wallet balance is insufficient"""
        mock_assign_request = AssignRequest(
            bundle_code="BUNDLE-001",
            promo_code=None,
            affiliate_code=None,
            payment_type=PaymentTypeEnum.WALLET,
            related_search=RelatedSearchRequestDto(
                countries=[CountryRequestDto(country_name="Lebanon", iso3_code="LBN")]),

        )

        user_service._UserBundleService__esim_hub_service.get_bundle_by_id.return_value = mock_bundle

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.ASSIGN,
            amount=1000,
            modified_amount=1000,
            currency="USD",
            bundle_data=mock_bundle.model_dump_json(),
            payment_type=PaymentTypeEnum.WALLET,
        )
        user_service._UserBundleService__user_order_repo.create.return_value = mock_order

        # Mock insufficient wallet balance
        poor_wallet = UserWalletModel(
            id="wallet-123",
            user_id=mock_user.id,
            amount=5.00,  # Only $5, need $10
            currency="USD",
        )
        user_service._UserBundleService__user_wallet_service.get_user_wallet.return_value = poor_wallet
        user_service._UserBundleService__currency_service.get_currency_rate.return_value = 1.0

        with pytest.raises(CustomException) as exc_info:
            await user_service.assign(
                user=mock_user,
                device_id="device-123",
                assign_request=mock_assign_request,
                x_currency="USD",
                locale="en",
                request=mock_request,
            )

        assert exc_info.value.code == 400
        assert ErrorMessages.INSUFFICIENT_WALLET_BALANCE in exc_info.value.name

    @pytest.mark.asyncio
    async def test_assign_with_dcb_payment(
            self, user_service, mock_user, mock_bundle, mock_request
    ):
        """Test assignment with DCB (Direct Carrier Billing) payment"""
        mock_assign_request = AssignRequest(
            bundle_code="BUNDLE-001",
            promo_code=None,
            affiliate_code=None,
            payment_type=PaymentTypeEnum.DCB,
            related_search=RelatedSearchRequestDto(
                countries=[CountryRequestDto(country_name="Lebanon", iso3_code="LBN")]),

        )

        user_service._UserBundleService__esim_hub_service.get_bundle_by_id.return_value = mock_bundle

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.ASSIGN,
            amount=1000,
            modified_amount=1000,
            currency="USD",
            bundle_data=mock_bundle.model_dump_json(),
            payment_type=PaymentTypeEnum.DCB,
        )
        user_service._UserBundleService__user_order_repo.create.return_value = mock_order

        mock_user_copy = UsersCopyModel(
            id=mock_user.id,
            email=mock_user.email,
            metadata={"language": "en"},
        )
        user_service._UserBundleService__user_repo.get_by_id.return_value = mock_user_copy

        with patch("app.services.user_service.generate_otp") as mock_otp:
            mock_otp.return_value = "123456"

            with patch("app.services.user_service.get_config") as mock_config:
                mock_config.return_value = "5"

                result = await user_service.assign(
                    user=mock_user,
                    device_id="device-123",
                    assign_request=mock_assign_request,
                    x_currency="USD",
                    locale="en",
                    request=mock_request,
                )

        assert result.status == "success"
        assert result.data.payment_status == PaymentStatusEnum.PENDING_VERIFICATION
        user_service._UserBundleService__dcb_service.send_otp.assert_called_once()



# ============================================================================
# ASSIGN TOP-UP TESTS
# ============================================================================


class TestAssignTopUp:
    """Tests for the assign_top_up method"""

    @pytest.mark.asyncio
    async def test_assign_top_up_with_tax(
            self, user_service, mock_user, mock_bundle, mock_request
    ):
        """Test top-up assignment with tax calculation"""
        top_up_request = AssignTopUpRequest(
            bundle_code="BUNDLE-001",
            iccid="8901234567890123456",
            payment_type=PaymentTypeEnum.CARD,
        )

        mock_bundle_response = Mock()
        mock_bundle_response.data = mock_bundle
        user_service._UserBundleService__bundle_service.get_bundle.return_value = mock_bundle_response

        mock_order = UserOrderModel(
            id="order-topup-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.BUNDLE_TOP_UP,
            amount=1000,
            modified_amount=1000,
            currency="USD",
            bundle_data=mock_bundle.model_dump_json(),
            payment_type=PaymentTypeEnum.CARD,
        )
        user_service._UserBundleService__user_order_repo.create.return_value = mock_order

        # Mock currency conversions
        user_service._UserBundleService__currency_service.get_currency_rate.return_value = 1.2  # EUR rate
        user_service._UserBundleService__currency_service.convert.return_value = 1.67  # Tax in USD

        with patch("app.services.user_service.create_payment_intent") as mock_create_payment:
            mock_payment_intent = Mock()
            mock_payment_intent.id = "pi_topup_123"
            mock_payment_intent.client_secret = "secret_topup_123"
            mock_payment_intent.customer = "cus_123"
            mock_payment_intent.livemode = True
            mock_payment_intent.amount = 1200  # 1000 + 200 tax in cents

            mock_tax = Mock()
            mock_tax.tax_amount_exclusive = 200  # 200 cents = $2.00 tax

            mock_create_payment.return_value = (mock_payment_intent, mock_tax)

            with patch("app.services.user_service.create_payment_ephemeral") as mock_ephemeral:
                mock_ephemeral_key = Mock()
                mock_ephemeral_key.secret = "eph_secret_topup_123"
                mock_ephemeral.return_value = mock_ephemeral_key

                result = await user_service.assign_top_up(
                    user=mock_user,
                    assign_top_up_request=top_up_request,
                    device_id="device-123",
                    request=mock_request,
                    x_currency="EUR",
                    locale="en",
                )

        assert result.status == "success"
        assert result.data.has_tax is True

        # Verify convert was called to convert tax back to USD
        user_service._UserBundleService__currency_service.convert.assert_called_with(
            "EUR",  # from currency
            "USD",  # to currency (order.currency)
            2.0  # tax amount in EUR
        )

        # Verify order was updated with converted tax (1.67 USD * 100 = 167 cents)
        user_service._UserBundleService__user_order_repo.update.assert_called_with(
            record_id="order-topup-123",
            data={"tax_amount": 167}  # 1.67 * 100 rounded
        )

# ============================================================================
# GET USER ESIMS TESTS
# ============================================================================


class TestGetUserEsims:
    """Tests for retrieving user eSIMs"""

    @pytest.mark.asyncio
    async def test_get_user_esims_success(self, user_service, mock_user, mock_bundle):
        """Test successful retrieval of user eSIMs"""
        # Mock profile with bundles
        mock_profile_data = {
            "id": "profile-123",
            "user_id": mock_user.id,
            "user_order_id": "order-123",
            "iccid": "8901234567890123456",
            "validity": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "user_profile_bundle": [
                {
                    "id": "bundle-123",
                    "user_id": mock_user.id,
                    "user_profile_id": "profile-123",
                    "iccid": "8901234567890123456",
                    "bundle_data": mock_bundle.model_dump(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

        user_service._UserBundleService__user_profile_repo.select.return_value = [mock_profile_data]
        user_service._UserBundleService__currency_service.get_rate_by_currency.return_value = 1.0
        user_service._UserBundleService__bundle_translation_repo.get_first_by.return_value = None

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.ASSIGN,
            amount=1000,
            modified_amount=1000,
            currency="USD",
            tax_amount=0,
        )
        user_service._UserBundleService__user_order_repo.get_by_id.return_value = mock_order

        result = await user_service.get_user_esims(
            user=mock_user,
            x_currency="USD",
            accept_language="en",
        )

        assert result.status == "success"
        assert isinstance(result.data, list)

    @pytest.mark.asyncio
    async def test_get_user_esims_no_profiles(self, user_service, mock_user):
        """Test retrieving eSIMs when user has no profiles"""
        user_service._UserBundleService__user_profile_repo.select.return_value = []
        user_service._UserBundleService__user_profile_bundle_repo.list.return_value = []

        result = await user_service.get_user_esims(
            user=mock_user,
            x_currency="USD",
            accept_language="en",
        )

        assert result.status == "success"
        assert len(result.data) == 0


# ============================================================================
# CONSUMPTION TESTS
# ============================================================================


class TestConsumption:
    """Tests for bundle consumption retrieval"""

    @pytest.mark.asyncio
    async def test_consumption_with_started_bundle(self, user_service, mock_user):
        """Test consumption retrieval with started bundle"""
        iccid = "8901234567890123456"

        mock_started_bundle = UserProfileBundleModel(
            id=123,
            user_id=mock_user.id,
            user_order_id="order-123",
            user_profile_id="profile-123",
            bundle_type="Primary Bundle",
            iccid=iccid,
            bundle_expired=False,
            plan_started=True,
            esim_hub_order_id="esim-order-123",
            created_at="2025-01-01 00:00:00",
            bundle_data={},
        )

        user_service._UserBundleService__user_profile_bundle_repo.list.return_value = [mock_started_bundle]

        mock_profile = UserProfileModel(
            id="profile-123",
            user_id=mock_user.id,
            iccid=iccid,
            esim_hub_order_id="esim-order-123",
            user_order_id="order-123",
            validity="7",
            created_at="2025-01-01 00:00:00",
            smdp_address="smdp_address",
            allow_topup=True

        )
        user_service._UserBundleService__user_profile_repo.get_first_by.return_value = mock_profile

        mock_consumption = ConsumptionResponse(
            data_used=500000000,  # 500MB
            total_data=1000000000,  # 1GB
            data_remaining=500000000,
            data_allocated=1000000000,
            data_allocated_display="1 GB",
            data_remaining_display="500 MB",
            data_used_display="500 MB",
            plan_status="started",
            expiry_date="2025-12-01 00:00:00",


        )
        user_service._UserBundleService__esim_hub_service.get_bundle_consumption.return_value = mock_consumption

        result = await user_service.consumption(user=mock_user, iccid=iccid)

        assert result.status == "success"
        print("result:", result)
        assert result.data.data_used == 500000000

    @pytest.mark.asyncio
    async def test_consumption_profile_not_found(self, user_service, mock_user):
        """Test consumption when profile is not found"""
        iccid = "8901234567890123456"

        user_service._UserBundleService__user_profile_bundle_repo.list.return_value = []
        user_service._UserBundleService__user_profile_repo.get_first_by.return_value = None

        with pytest.raises(CustomException) as exc_info:
            await user_service.consumption(user=mock_user, iccid=iccid)

        assert exc_info.value.code == 400
        assert ErrorMessages.USER_PROFILE_NOT_FOUND in exc_info.value.name


# ============================================================================
# BUNDLE NAME UPDATE TESTS
# ============================================================================


class TestUpdateBundleName:
    """Tests for updating bundle labels"""

    @pytest.mark.asyncio
    async def test_update_bundle_name_success(self, user_service, mock_user, mock_bundle):
        """Test successful bundle name update"""
        bundle_code = "BUNDLE-001"
        new_label = "My Vacation Bundle"

        mock_profile_bundle = UserProfileBundleModel(
            id=123,

            user_id=mock_user.id,
            user_order_id="user_order_id",
            user_profile_id="user_profile_id",
            esim_hub_order_id="esim_hub_order_id",
            bundle_type=UserBundleType.PRIMARY_BUNDLE,
            plan_started=True,
            bundle_expired=False,


            iccid="11111",
            bundle_data=mock_bundle.model_dump(),
            created_at="20205-01-01T00:00:00Z",
        )

        user_service._UserBundleService__user_profile_bundle_repo.get_first_by.return_value = mock_profile_bundle

        label_request = UpdateBundleLabelRequest(label=new_label)

        result = await user_service.update_bundle_name(
            code=bundle_code,
            bundle_label_request=label_request,
            user=mock_user,
        )

        assert result.status == "success"
        user_service._UserBundleService__user_profile_bundle_repo.update_by.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_bundle_name_not_found(self, user_service, mock_user):
        """Test bundle name update when bundle not found"""
        bundle_code = "NON-EXISTENT"

        user_service._UserBundleService__user_profile_bundle_repo.get_first_by.return_value = None

        label_request = UpdateBundleLabelRequest(label="New Label")

        with pytest.raises(CustomException) as exc_info:
            await user_service.update_bundle_name(
                code=bundle_code,
                bundle_label_request=label_request,
                user=mock_user,
            )

        assert exc_info.value.code == 400
        assert ErrorMessages.USER_PROFILE_BUNDLE_NOT_FOUND in exc_info.value.name

    @pytest.mark.asyncio
    async def test_update_bundle_name_by_iccid(self, user_service, mock_user, mock_bundle):
        """Test bundle name update by ICCID"""
        iccid = "8901234567890123456"
        new_label = "Updated Label"

        mock_profile_bundle = UserProfileBundleModel(
            id=123,
            user_order_id="user_order_id",
            user_profile_id="user_profile_id",
            esim_hub_order_id="esim_hub_order_id",
            bundle_type=UserBundleType.PRIMARY_BUNDLE,
            plan_started=True,
            bundle_expired=False,

            user_id=mock_user.id,
            iccid=iccid,
            bundle_data=mock_bundle.model_dump(),
            created_at="20205-01-01T00:00:00Z",
        )

        user_service._UserBundleService__user_profile_bundle_repo.get_first_by.return_value = mock_profile_bundle

        label_request = UpdateBundleLabelRequest(label=new_label)

        result = await user_service.update_bundle_name_by_iccid(
            iccid=iccid,
            bundle_label_request=label_request,
            user=mock_user,
        )

        assert result.status =="success"


# ============================================================================
# OTP VERIFICATION TESTS
# ============================================================================


class TestVerifyOrderOtp:
    """Tests for OTP verification in DCB payments"""

    @pytest.mark.asyncio
    async def test_verify_otp_success(self, user_service, mock_user, mock_bundle):
        """Test successful OTP verification"""
        otp_request = VerifyOtpRequestDto(
            order_id="order-123",
            otp="123456",
            iccid="8901234567890123456",
        )

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            bundle_id=mock_bundle.bundle_code,
            order_type=UserOrderType.ASSIGN,
            amount=1000,
            modified_amount=1000,
            currency="USD",
            bundle_data=mock_bundle.model_dump_json(),
            otp="123456",
            otp_expired_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )

        user_service._UserBundleService__user_order_repo.get_by_id.return_value = mock_order

        mock_user_copy = UsersCopyModel(
            id=mock_user.id,
            email=mock_user.email,
            metadata={"language": "en"},
        )
        user_service._UserBundleService__user_repo.get_by_id.return_value = mock_user_copy

        user_service._UserBundleService__currency_service.convert.return_value = 10.00
        user_service._UserBundleService__dcb_service.deduct_balance.return_value = True

        # CRITICAL FIX: AsyncMock must have a return_value that is a Response object
        user_service._UserBundleService__bundle_service.buy_bundle = AsyncMock(
            return_value=ResponseHelper.success_data_response(True, 0)
        )

        with patch("app.services.user_service.supabase_client") as mock_supabase:
            mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.lt.return_value.execute.return_value.data = []

            result = await user_service.verify_order_otp(
                user=mock_user,
                request=otp_request,
            )

        print("result", result)

        # Assertions
        assert result.status == "success"
        assert result.data is True

        # Verify deduct_balance was called with correct parameters
        user_service._UserBundleService__dcb_service.deduct_balance.assert_called_once_with(
            msisdn=mock_user.msisdn,
            amount=10.00,
            order_id="order-123",
            locale="en"
        )

        # Verify buy_bundle was called
        user_service._UserBundleService__bundle_service.buy_bundle.assert_called_once()

        # Verify the specific parameters passed to buy_bundle
        call_args = user_service._UserBundleService__bundle_service.buy_bundle.call_args
        assert call_args.kwargs['user_id'] == mock_user.id
        assert call_args.kwargs['payment_status'] == OrderStatusEnum.SUCCESS
        assert call_args.kwargs['payment_type'] == PaymentTypeEnum.DCB

    @pytest.mark.asyncio
    async def test_verify_otp_invalid(self, user_service, mock_user):
        """Test OTP verification with invalid OTP"""
        otp_request = VerifyOtpRequestDto(
            order_id="order-123",
            otp="999999",  # Wrong OTP
            iccid="8901234567890123456",
        )

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            otp="123456",  # Correct OTP is different
            amount=1,
            currency="USD",
            otp_expired_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )

        user_service._UserBundleService__user_order_repo.get_by_id.return_value = mock_order

        with pytest.raises(CustomException) as exc_info:
            await user_service.verify_order_otp(
                user=mock_user,
                request=otp_request,
            )

        assert exc_info.value.code == 404
        assert ErrorMessages.OTP_INVALID in exc_info.value.name

    @pytest.mark.asyncio
    async def test_verify_otp_expired(self, user_service, mock_user):
        """Test OTP verification with expired OTP"""
        otp_request = VerifyOtpRequestDto(
            order_id="order-123",
            otp="123456",
            iccid="8901234567890123456",
        )

        mock_order = UserOrderModel(
            id="order-123",
            user_id=mock_user.id,
            otp="123456",
            amount=1,
            currency="USD",
            otp_expired_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),  # Expired
        )

        user_service._UserBundleService__user_order_repo.get_by_id.return_value = mock_order

        with patch("app.services.user_service.supabase_client") as mock_supabase:
            # Mock expired OTP check
            mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.lt.return_value.execute.return_value.data = [
                {"id": "order-123"}
            ]

            with pytest.raises(CustomException) as exc_info:
                await user_service.verify_order_otp(
                    user=mock_user,
                    request=otp_request,
                )

        assert exc_info.value.code == 400
        assert ErrorMessages.OTP_EXPIRED in exc_info.value.name


# ============================================================================
# CANCEL ORDER TESTS
# ============================================================================


class TestCancelOrder:
    """Tests for order cancellation"""

    @pytest.mark.asyncio
    async def test_cancel_order_success(self, user_service, mock_user):
        """Test successful order cancellation"""
        order_id = "order-123"

        mock_order = UserOrderModel(
            id=order_id,
            user_id=mock_user.id,
            payment_intent_code="pi_123",
            order_status=OrderStatusEnum.PENDING,
            payment_status=OrderStatusEnum.PENDING,
            currency="USD",
            amount=1
        )

        user_service._UserBundleService__user_order_repo.get_first_by.return_value = mock_order

        with patch("app.services.user_service.stripe.PaymentIntent.cancel") as mock_stripe_cancel:
            result = await user_service.cancel_order(
                order_id=order_id,
                user=mock_user,
            )

        assert result.status == "success"
        user_service._UserBundleService__user_order_repo.update.assert_called_once()
        user_service._UserBundleService__promotion_service.cancel_promotion_usage.assert_called_once_with(
            order_id=order_id
        )
        mock_stripe_cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_order_not_found(self, user_service, mock_user):
        """Test cancellation of non-existent order"""
        order_id = "non-existent-order"

        user_service._UserBundleService__user_order_repo.get_first_by.return_value = None

        with pytest.raises(CustomException) as exc_info:
            await user_service.cancel_order(
                order_id=order_id,
                user=mock_user,
            )

        assert exc_info.value.code == 404
        assert ErrorMessages.ORDER_NOT_FOUND in exc_info.value.name


# ============================================================================
# ORDER HISTORY TESTS
# ============================================================================


class TestGetOrderHistory:
    """Tests for retrieving order history"""

    @pytest.mark.asyncio
    async def test_get_order_history_success(self, user_service, mock_user, mock_bundle):
        """Test successful order history retrieval"""
        mock_orders = [
            UserOrderModel(
                id="order-1",
                user_id=mock_user.id,
                bundle_id=mock_bundle.bundle_code,
                bundle_data=mock_bundle.model_dump_json(),
                payment_status=OrderStatusEnum.SUCCESS,
                order_status=OrderStatusEnum.SUCCESS,
                amount=1000,
                modified_amount=1000,
                currency="USD",
                created_at="2025-01-01T00:00:00+00:00",
            ),
            UserOrderModel(
                id="order-2",
                user_id=mock_user.id,
                bundle_id=mock_bundle.bundle_code,
                bundle_data=mock_bundle.model_dump_json(),
                payment_status=OrderStatusEnum.SUCCESS,
                order_status=OrderStatusEnum.SUCCESS,
                amount=2000,
                modified_amount=2000,
                currency="USD",
                created_at="2025-01-01T00:00:00+00:00",
            ),
        ]

        user_service._UserBundleService__user_order_repo.list.return_value = mock_orders
        user_service._UserBundleService__currency_service.get_currency_rate.return_value = 1.0
        user_service._UserBundleService__bundle_translation_repo.get_first_by.return_value = None

        result = await user_service.get_order_history(
            user_id=mock_user.id,
            page_index=1,
            page_size=10,
            x_currency="USD",
            accept_language="en",
        )

        assert result.status=="success"
        assert len(result.data) == 2

    @pytest.mark.asyncio
    async def test_get_order_history_empty(self, user_service, mock_user):
        """Test order history retrieval with no orders"""
        user_service._UserBundleService__user_order_repo.list.return_value = []

        result = await user_service.get_order_history(
            user_id=mock_user.id,
            page_index=1,
            page_size=10,
            x_currency="USD",
            accept_language="en",
        )

        assert result.status =="success"
        assert len(result.data) == 0


