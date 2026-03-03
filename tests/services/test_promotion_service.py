"""
Unit Tests for PromotionService

These are pure unit tests with ALL external dependencies mocked.
No database connections, API calls, or external services are accessed.
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch, MagicMock, call
from typing import Literal

# Mock external dependencies before imports
import sys



# ============================================================================
# TEST FIXTURES
# ============================================================================

class TestPromotionServiceFixtures:
    """Base fixtures for PromotionService tests"""

    @pytest.fixture
    def mock_repos(self):
        """Mock all repository dependencies"""
        repos = Mock()
        repos.promotion_repo = Mock()
        repos.promotion_rule_repo = Mock()
        repos.promotion_usage_repo = Mock()
        repos.user_repo = Mock()
        repos.bundle_repo = Mock()
        repos.user_profile_repo = Mock()

        # Prevent real database calls
        repos.promotion_repo.get_first_by = Mock(return_value=None)
        repos.promotion_repo.create = Mock(return_value=Mock(id="promo-123"))
        repos.promotion_repo.update_by = Mock(return_value=True)

        repos.promotion_rule_repo.get_first_by = Mock(return_value=None)
        repos.promotion_rule_repo.get_by_id = Mock(return_value=None)

        repos.promotion_usage_repo.create = Mock(return_value=Mock(id="usage-123"))
        repos.promotion_usage_repo.list = Mock(return_value=[])
        repos.promotion_usage_repo.get_first_by = Mock(return_value=None)
        repos.promotion_usage_repo.update_by = Mock(return_value=True)
        repos.promotion_usage_repo.select_procedure = Mock(return_value=[])

        repos.user_repo.get_by_id = Mock(return_value=None)
        repos.user_repo.get_first_by = Mock(return_value=None)
        repos.user_repo.referral_code_key = Mock(return_value="metadata.referral_code")

        repos.user_profile_repo.list = Mock(return_value=[])

        return repos

    @pytest.fixture
    def mock_services(self):
        """Mock all service dependencies"""
        services = Mock()
        services.user_wallet_service = Mock()
        services.currency_service = Mock()
        services.bundle_service = Mock()

        # Configure service methods
        services.user_wallet_service.get_wallet_transactions = Mock(return_value=[])
        services.user_wallet_service.add_wallet_transaction = Mock(return_value=True)

        services.currency_service.get_currency_rate = Mock(return_value=1.0)
        services.currency_service.get_rate_by_currency = Mock(return_value=1.0)

        return services

    @pytest.fixture
    def promotion_service(self, mock_repos, mock_services):
        """Create PromotionService with mocked dependencies"""
        with patch.dict('os.environ', {
            'SYSTEM_CURRENCY': 'USD',
            'DEFAULT_CURRENCY': 'USD',
        }, clear=False), \
                patch('app.services.promotion_service.PromotionRepo') as mock_promo_repo_class, \
                patch('app.services.promotion_service.PromotionRuleRepo') as mock_rule_repo_class, \
                patch('app.services.promotion_service.PromotionUsageRepo') as mock_usage_repo_class, \
                patch('app.services.promotion_service.UserRepo') as mock_user_repo_class, \
                patch('app.services.promotion_service.BundleRepo') as mock_bundle_repo_class, \
                patch('app.services.promotion_service.UserProfileRepo') as mock_profile_repo_class, \
                patch('app.services.promotion_service.UserWalletService') as mock_wallet_service_class, \
                patch('app.services.promotion_service.CurrencyService') as mock_currency_service_class:
            # Configure mock returns
            mock_promo_repo_class.return_value = mock_repos.promotion_repo
            mock_rule_repo_class.return_value = mock_repos.promotion_rule_repo
            mock_usage_repo_class.return_value = mock_repos.promotion_usage_repo
            mock_user_repo_class.return_value = mock_repos.user_repo
            mock_bundle_repo_class.return_value = mock_repos.bundle_repo
            mock_profile_repo_class.return_value = mock_repos.user_profile_repo
            mock_wallet_service_class.return_value = mock_services.user_wallet_service
            mock_currency_service_class.return_value = mock_services.currency_service

            # Import and create service AFTER mocking
            from app.services.promotion_service import PromotionService
            service = PromotionService()

            # Store references for easier test access
            service._test_repos = mock_repos
            service._test_services = mock_services

            yield service

    @pytest.fixture
    def bundle_dto(self):
        """Mock BundleDTO"""
        from app.schemas.home import BundleDTO, BundleCategoryDTO, CountryDTO

        bundle = Mock(spec=BundleDTO)
        bundle.bundle_code = "bundle-123"
        bundle.display_title = "5GB Data Plan"
        bundle.original_price = 10.0
        bundle.price = 10.0
        bundle.price_display = "10.00 USD"
        bundle.currency_code = "USD"
        bundle.bundle_category = Mock(spec=BundleCategoryDTO)
        bundle.countries = []
        return bundle

    @pytest.fixture
    def promotion_model(self):
        """Mock PromotionModel"""
        promo = Mock()
        promo.id = "promo-123"
        promo.code = "SAVE10"
        promo.amount = 10.0
        promo.is_active = True
        promo.valid_from = "2025-01-01"
        promo.valid_to = "2030-12-31"
        promo.times_used = 0
        promo.rule_id = "rule-123"
        promo.bundle_code = ""
        return promo

    @pytest.fixture
    def promotion_rule_model(self):
        """Mock PromotionRuleModel"""
        rule = Mock()
        rule.id = "rule-123"
        rule.promotion_rule_action_id = 1  # DISCOUNT_AMOUNT
        rule.promotion_rule_event_id = 1  # CREATE_ORDER
        rule.beneficiary = 1  # REFERRER
        rule.max_usage = 100
        return rule

    @pytest.fixture
    def user_model(self):
        """Mock UsersCopyModel"""
        user = Mock()
        user.id = "user-123"
        user.email = "test@example.com"
        user.metadata = {
            "referral_code": "REF123",
            "first_name": "John",
            "last_name": "Doe"
        }
        return user

    @pytest.fixture
    def promotion_usage_model(self):
        """Mock PromotionUsageModel"""
        usage = Mock()
        usage.id = "usage-123"
        usage.user_id = "user-123"
        usage.amount = 10.0
        usage.promotion_code = "SAVE10"
        usage.referral_code = None
        usage.status = "pending"
        usage.bundle_id = "bundle-123"
        usage.created_at = datetime.now(timezone.utc)
        return usage


# ============================================================================
# TEST CASES - VALIDATION
# ============================================================================

class TestValidatePromoCode(TestPromotionServiceFixtures):
    """Test cases for validate_promo_code method"""

    @pytest.mark.asyncio
    async def test_validate_promo_code_discount_amount(
            self,
            promotion_service,
            bundle_dto,
            promotion_model,
            promotion_rule_model
    ):
        """Test validating promo code with discount amount"""
        # Setup mocks
        promotion_rule_model.promotion_rule_action_id = 1  # DISCOUNT_AMOUNT
        promotion_model.amount = 5.0

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_services.currency_service.get_rate_by_currency.return_value = 1.0

        with patch('app.services.promotion_service.I18n') as mock_i18n:
            mock_i18n.get_message.return_value = "Discount Amount"
            with patch('app.services.promotion_service.get_config') as config_mock:

                # Execute
                result = await promotion_service.validate_promo_code(
                    code="SAVE10",
                    user_id="user-123",
                    bundle=bundle_dto,
                    device_id="device-123",
                    currency="USD",
                    apply_usage=False
            )

        # Verify
        assert result is not None
        assert result.bundle.original_price == 5.0  # 10.0 - 5.0
        assert "Discount Amount" in result.message
        assert result.rule_id == "rule-123"


    @pytest.mark.asyncio
    async def test_validate_promo_code_discount_percentage(
            self,
            promotion_service,
            bundle_dto,
            promotion_model,
            promotion_rule_model
    ):
        """Test validating promo code with discount percentage"""
        # Setup mocks
        promotion_rule_model.promotion_rule_action_id = 2  # DISCOUNT_PERCENTAGE
        promotion_model.amount = 20.0  # 20% discount

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_services.currency_service.get_rate_by_currency.return_value = 1.0
        with patch('app.services.promotion_service.get_config') as mock_config, \
                patch('app.services.promotion_service.I18n') as mock_i18n:
            mock_i18n.get_message.return_value = "Discount Percentage"

            # Execute
            result = await promotion_service.validate_promo_code(
                code="SAVE20",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=False
            )

        # Verify
        assert result is not None
        assert result.bundle.original_price == 8.0  # 10.0 - (10.0 * 0.20)
        assert "Discount Percentage" in result.message

    @pytest.mark.asyncio
    async def test_validate_promo_code_cashback_amount(
            self,
            promotion_service,
            bundle_dto,
            promotion_model,
            promotion_rule_model
    ):
        """Test validating promo code with cashback amount"""
        # Setup mocks
        promotion_rule_model.promotion_rule_action_id = 3  # CASHBACK_AMOUNT
        promotion_model.amount = 5.0

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_services.currency_service.get_rate_by_currency.return_value = 1.0

        with patch('app.services.promotion_service.get_config') as mock_config, \
                patch('app.services.promotion_service.I18n') as mock_i18n:
            mock_i18n.get_message.return_value = "Cashback Amount"

            # Execute
            result = await promotion_service.validate_promo_code(
                code="CASHBACK5",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=False
            )

        # Verify
        assert result is not None
        assert result.bundle.original_price == 10.0  # Price unchanged for cashback
        assert "Cashback Amount" in result.message

    @pytest.mark.asyncio
    async def test_validate_promo_code_not_found(
            self,
            promotion_service,
            bundle_dto
    ):
        """Test validating non-existent promo code"""
        # Setup mocks - return None for non-existent promo
        promotion_service._test_repos.promotion_repo.get_first_by.return_value = None
        promotion_service._test_repos.user_repo.get_first_by.return_value = None

        from app.exceptions import CustomException

        # Execute and verify exception
        with pytest.raises(CustomException) as exc_info:
            await promotion_service.validate_promo_code(
                code="INVALID",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=False
            )

        assert exc_info.value.code == 400

    @pytest.mark.asyncio
    async def test_validate_promo_code_bundle_mismatch(
            self,
            promotion_service,
            bundle_dto,
            promotion_model,
            promotion_rule_model
    ):
        """Test promo code with bundle restriction"""
        # Setup mocks - promo only valid for specific bundle
        promotion_model.bundle_code = "bundle-456,bundle-789"
        bundle_dto.bundle_code = "bundle-123"  # Different bundle

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model

        from app.exceptions import CustomException

        # Execute and verify exception
        with pytest.raises(CustomException) as exc_info:
            await promotion_service.validate_promo_code(
                code="SAVE10",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=False
            )

        assert exc_info.value.code == 400

    @pytest.mark.asyncio
    async def test_validate_promo_code_with_apply_usage(
            self,
            promotion_service,
            bundle_dto,
            promotion_model,
            promotion_rule_model
    ):
        """Test applying promo code creates usage record"""
        # Setup mocks
        promotion_rule_model.promotion_rule_action_id = 1  # DISCOUNT_AMOUNT
        promotion_model.amount = 5.0

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_services.currency_service.get_rate_by_currency.return_value = 1.0

        with patch('app.services.promotion_service.get_config') as mock_config, \
                patch('app.services.promotion_service.I18n') as mock_i18n:
            mock_i18n.get_message.return_value = "Discount Applied"

            # Execute with apply_usage=True
            result = await promotion_service.validate_promo_code(
                code="SAVE10",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=True,
                order_id="order-123"
            )

        # Verify usage record was created
        promotion_service._test_repos.promotion_usage_repo.create.assert_called_once()
        call_args = promotion_service._test_repos.promotion_usage_repo.create.call_args[1]['data']
        assert call_args['user_id'] == "user-123"
        assert call_args['promotion_code'] == "SAVE10"
        assert call_args['status'] == "pending"


# ============================================================================
# TEST CASES - REFERRAL CODE
# ============================================================================

class TestReferralCodeValidation(TestPromotionServiceFixtures):
    """Test cases for referral code validation"""


    @pytest.mark.asyncio
    async def test_validate_referral_code_own_code(
            self,
            promotion_service,
            bundle_dto,
            user_model
    ):
        """Test user cannot use their own referral code"""
        # Setup mocks - user tries to use their own code
        user_model.metadata["referral_code"] = "REF123"

        promotion_service._test_repos.user_repo.get_first_by.return_value = user_model
        promotion_service._test_repos.user_repo.get_by_id.return_value = user_model
        promotion_service._test_repos.user_profile_repo.list.return_value = []

        from app.exceptions import CustomException

        with patch('app.services.promotion_service.get_config') as mock_config:
            mock_config.side_effect = lambda key, default=None: {
                'DEFAULT_REFERRAL_RULE_ID': 'rule-123',
                'REFERRAL_CODE_PERCENTAGE': '20',
                'REFERRAL_CODE_AMOUNT': '5.0'
            }.get(key, default)

            # Execute and verify exception
            with pytest.raises(CustomException) as exc_info:
                await promotion_service.validate_promo_code(
                    code="REF123",  # User's own code
                    user_id="user-123",
                    bundle=bundle_dto,
                    device_id="device-123",
                    currency="USD",
                    apply_usage=False
                )

        assert exc_info.value.code == 400

    @pytest.mark.asyncio
    async def test_validate_referral_code_already_purchased(
            self,
            promotion_service,
            bundle_dto,
            user_model
    ):
        """Test referral code rejected if user already purchased"""
        # Setup mocks - user has previous purchase
        previous_profile = Mock()
        previous_profile.id = "profile-123"

        referrer_user = Mock()
        referrer_user.id = "referrer-456"

        promotion_service._test_repos.user_repo.get_first_by.return_value = referrer_user
        promotion_service._test_repos.user_repo.get_by_id.return_value = user_model
        promotion_service._test_repos.user_profile_repo.list.return_value = [previous_profile]

        from app.exceptions import CustomException

        with patch('app.services.promotion_service.get_config') as mock_config:
            mock_config.side_effect = lambda key, default=None: {
                'DEFAULT_REFERRAL_RULE_ID': 'rule-123',
                'REFERRAL_CODE_PERCENTAGE': '20',
                'REFERRAL_CODE_AMOUNT': '5.0'
            }.get(key, default)

            # Execute and verify exception
            with pytest.raises(CustomException) as exc_info:
                await promotion_service.validate_promo_code(
                    code="REF456",
                    user_id="user-123",
                    bundle=bundle_dto,
                    device_id="device-123",
                    currency="USD",
                    apply_usage=False
                )

        assert exc_info.value.code == 400

    def test_is_referral_code_true(self, promotion_service, user_model):
        """Test is_referral_code returns True for valid referral"""
        promotion_service._test_repos.user_repo.get_first_by.return_value = user_model

        result = promotion_service.is_referral_code("REF123")

        assert result is True

    def test_is_referral_code_false(self, promotion_service):
        """Test is_referral_code returns False for invalid code"""
        promotion_service._test_repos.user_repo.get_first_by.return_value = None

        result = promotion_service.is_referral_code("INVALID")

        assert result is False


# ============================================================================
# TEST CASES - PROMOTION VALIDATION
# ============================================================================

class TestPromotionValidation(TestPromotionServiceFixtures):
    """Test cases for promotion validation logic"""

    @pytest.mark.asyncio
    async def test_validate_promotion_not_active(
            self,
            promotion_service,
            promotion_model,
            promotion_rule_model,
            bundle_dto
    ):
        """Test promotion validation fails when inactive"""
        # Setup mocks - inactive promotion
        promotion_model.is_active = False

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model

        from app.exceptions import CustomException

        # Execute and verify exception
        with pytest.raises(CustomException) as exc_info:
            await promotion_service.validate_promo_code(
                code="INACTIVE",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=False
            )

        assert exc_info.value.code == 400

    @pytest.mark.asyncio
    async def test_validate_promotion_max_usage_reached(
            self,
            promotion_service,
            promotion_model,
            promotion_rule_model,
            bundle_dto
    ):
        """Test promotion validation fails when max usage reached"""
        # Setup mocks - max usage reached
        promotion_model.times_used = 100
        promotion_rule_model.max_usage = 100

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model

        from app.exceptions import CustomException

        # Execute and verify exception
        with pytest.raises(CustomException) as exc_info:
            await promotion_service.validate_promo_code(
                code="MAXED",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=False
            )

        assert exc_info.value.code == 400



    @pytest.mark.asyncio
    async def test_validate_promotion_already_used_by_user(
            self,
            promotion_service,
            promotion_model,
            promotion_rule_model,
            bundle_dto,
            promotion_usage_model
    ):
        """Test promotion validation fails when already used"""
        # Setup mocks - user already used this promo
        promotion_usage_model.status = "completed"

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_repos.promotion_usage_repo.list.return_value = [promotion_usage_model]

        from app.exceptions import CustomException

        with patch('app.services.promotion_service.get_config', return_value="true"):
            # Execute and verify exception
            with pytest.raises(CustomException) as exc_info:
                await promotion_service.validate_promo_code(
                    code="USED",
                    user_id="user-123",
                    bundle=bundle_dto,
                    device_id="device-123",
                    currency="USD",
                    apply_usage=False
                )

        assert exc_info.value.code == 400


# ============================================================================
# TEST CASES - APPLY PROMOTION AFTER PURCHASE
# ============================================================================

class TestApplyPromotionAfterPurchase(TestPromotionServiceFixtures):
    """Test cases for applying promotion after purchase"""

    @pytest.mark.asyncio
    async def test_apply_promotion_completed_discount(
            self,
            promotion_service,
            promotion_rule_model,
            promotion_usage_model
    ):
        """Test applying completed promotion with discount"""
        # Setup mocks
        promotion_rule_model.promotion_rule_action_id = 1  # DISCOUNT_AMOUNT

        user_order = Mock()
        user_order.id = "order-123"
        user_order.promo_code = "SAVE10"
        user_order.referral_code = None
        user_order.modified_amount = 1000  # $10.00 in cents
        user_order.currency = "USD"

        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_repos.user_repo.get_first_by.return_value = None
        promotion_service._test_repos.promotion_usage_repo.get_first_by.return_value = promotion_usage_model
        promotion_service._test_repos.promotion_usage_repo.list.return_value = []

        # Execute
        await promotion_service.apply_promotion_code_after_purchase(
            user_id="user-123",
            status="completed",
            rule_id="rule-123",
            user_order=user_order
        )

        # Verify
        promotion_service._test_repos.promotion_usage_repo.update_by.assert_called()
        promotion_service._test_repos.promotion_repo.update_by.assert_called_once()




    @pytest.mark.asyncio
    async def test_apply_promotion_failed_status(
            self,
            promotion_service,
            promotion_rule_model
    ):
        """Test applying failed promotion updates status only"""
        user_order = Mock()
        user_order.id = "order-123"
        user_order.promo_code = "SAVE10"
        user_order.referral_code = None
        user_order.modified_amount = 1000
        user_order.currency = "USD"

        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_repos.user_repo.get_first_by.return_value = None

        # Execute with failed status
        await promotion_service.apply_promotion_code_after_purchase(
            user_id="user-123",
            status="failed",
            rule_id="rule-123",
            user_order=user_order
        )

        # Verify only status update, no wallet transaction
        promotion_service._test_repos.promotion_usage_repo.update_by.assert_called_once()
        promotion_service._test_services.user_wallet_service.add_wallet_transaction.assert_not_called()


# ============================================================================
# TEST CASES - UTILITY METHODS
# ============================================================================

class TestUtilityMethods(TestPromotionServiceFixtures):
    """Test cases for utility methods"""

    @pytest.mark.asyncio
    async def test_history(self, promotion_service):
        """Test getting promotion history"""
        # Setup mocks
        transaction1 = Mock()
        transaction1.source = "CASHBACK_PROMO"
        transaction1.amount = 10.0
        transaction1.created_at = "2025-01-01T00:00:00Z"
        transaction1.referred_to = None

        transaction2 = Mock()
        transaction2.source = "CASHBACK_REFERRAL"
        transaction2.amount = 5.0
        transaction2.created_at = "2025-01-01T00:00:00Z"
        transaction2.referred_to = "user@example.com"

        promotion_service._test_services.user_wallet_service.get_wallet_transactions.return_value = [
            transaction1,
            transaction2
        ]
        promotion_service._test_services.currency_service.get_currency_rate.return_value = 1.0

        with patch('app.services.promotion_service.truncate_two_decimals_decimal', side_effect=lambda x: round(x, 2)):
            # Execute
            result = await promotion_service.history(user_id="user-123", x_currency="USD")

        # Verify
        assert result.totalCount == 2
        assert len(result.data) == 2

    def test_get_promotion_by_code(self, promotion_service, promotion_model):
        """Test getting promotion by code"""
        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model

        result = promotion_service.get_promotion_by_code("SAVE10")

        assert result == promotion_model
        promotion_service._test_repos.promotion_repo.get_first_by.assert_called_once_with(
            where={"code": "SAVE10"}
        )

    def test_get_referral_rule(self, promotion_service, promotion_rule_model):
        """Test getting referral rule"""
        promotion_service._test_repos.promotion_rule_repo.get_by_id.return_value = promotion_rule_model

        with patch('app.services.promotion_service.get_config', return_value="rule-123"):
            result = promotion_service.get_referral_rule()

        assert result == promotion_rule_model

    def test_get_rule_by_id(self, promotion_service, promotion_rule_model):
        """Test getting rule by ID"""
        promotion_service._test_repos.promotion_rule_repo.get_by_id.return_value = promotion_rule_model

        result = promotion_service.get_rule_by_id("rule-123")

        assert result == promotion_rule_model

    def test_cancel_promotion_usage(self, promotion_service):
        """Test canceling promotion usage"""
        promotion_service.cancel_promotion_usage("order-123")

        promotion_service._test_repos.promotion_usage_repo.update_by.assert_called_once()
        call_args = promotion_service._test_repos.promotion_usage_repo.update_by.call_args
        assert call_args[1]['where']['order_id'] == "order-123"


# ============================================================================
# TEST CASES - REFERRAL INFO
# ============================================================================

class TestReferralInfo(TestPromotionServiceFixtures):
    """Test cases for referral_info method"""

    def test_referral_info_cashback_amount(self, promotion_service, promotion_rule_model):
        """Test getting referral info for cashback amount"""
        promotion_rule_model.promotion_rule_action_id = 3  # CASHBACK_AMOUNT

        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_services.currency_service.get_rate_by_currency.return_value = 1.0

        with patch('app.services.promotion_service.get_config') as mock_config, \
                patch('app.services.promotion_service.I18n') as mock_i18n:
            mock_config.side_effect = lambda key, default=None: {
                'DEFAULT_REFERRAL_RULE_ID': 'rule-123',
                'REFERRAL_CODE_AMOUNT': '10.0',
                'REFERRAL_CODE_PERCENTAGE': '20'
            }.get(key, default)
            mock_i18n.get_message.return_value = "Get {amount} {currency} credit"

            result = promotion_service.referral_info(x_currency="USD", locale="en")

        assert result.data.amount == 10.0
        assert result.data.currency == "USD"
        assert "Get" in result.data.message

    def test_referral_info_discount_percentage(self, promotion_service, promotion_rule_model):
        """Test getting referral info for discount percentage"""
        promotion_rule_model.promotion_rule_action_id = 2  # DISCOUNT_PERCENTAGE

        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model
        promotion_service._test_services.currency_service.get_rate_by_currency.return_value = 1.2

        with patch('app.services.promotion_service.get_config') as mock_config, \
                patch('app.services.promotion_service.I18n') as mock_i18n:
            mock_config.side_effect = lambda key, default=None: {
                'DEFAULT_REFERRAL_RULE_ID': 'rule-123',
                'REFERRAL_CODE_AMOUNT': '10.0',
                'REFERRAL_CODE_PERCENTAGE': '15'
            }.get(key, default)
            mock_i18n.get_message.return_value = "Get {percentage}% off"

            result = promotion_service.referral_info(x_currency="EUR", locale="en")

        assert result.data.amount == 12.0  # 10.0 * 1.2
        assert result.data.currency == "EUR"


# ============================================================================
# TEST CASES - EDGE CASES
# ============================================================================

class TestEdgeCases(TestPromotionServiceFixtures):
    """Test cases for edge cases and error handling"""

    @pytest.mark.asyncio
    async def test_validate_promo_code_price_below_minimum(
            self,
            promotion_service,
            bundle_dto,
            promotion_model,
            promotion_rule_model
    ):
        """Test promo code rejected when final price too low"""
        # Setup mocks - discount brings price below minimum
        bundle_dto.original_price = 0.75
        promotion_model.amount = 0.50
        promotion_rule_model.promotion_rule_action_id = 1  # DISCOUNT_AMOUNT

        promotion_service._test_repos.promotion_repo.get_first_by.return_value = promotion_model
        promotion_service._test_repos.promotion_rule_repo.get_first_by.return_value = promotion_rule_model

        from app.exceptions import CustomException

        # Execute and verify exception
        with pytest.raises(CustomException) as exc_info:
            await promotion_service.validate_promo_code(
                code="TOOBIG",
                user_id="user-123",
                bundle=bundle_dto,
                device_id="device-123",
                currency="USD",
                apply_usage=False
            )

        assert exc_info.value.code == 400


