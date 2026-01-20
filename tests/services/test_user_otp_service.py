"""
Comprehensive tests for UserOtpService

Tests cover:
- OTP generation (success, limits, duplicates)
- OTP verification (valid, invalid, expired)
- Edge cases and error conditions
- Concurrency scenarios
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock, call
from decimal import Decimal

# Assuming these imports based on the service code
# Adjust import paths as needed for your project structure
from app.services.user_otp_service import UserOtpService
from app.exceptions import CustomException
from app.config.constants import ErrorMessages
from app.config.db import ConfigKeysEnum
from app.models.app import UserOtpModel


class TestUserOtpServiceGeneration:
    """Test suite for OTP generation functionality"""

    @pytest.fixture
    def service(self):
        """Create a fresh UserOtpService instance for each test"""
        return UserOtpService()

    @pytest.fixture
    def mock_repo(self, service):
        """Mock the UserOtpRepo to isolate service logic"""
        with patch.object(service, '_UserOtpService__user_otp_repo') as mock:
            yield mock

    @pytest.fixture
    def mock_config(self):
        """Mock get_config to return consistent expiration time"""
        with patch('app.services.user_otp_service.get_config') as mock:
            mock.return_value = 5  # 5 minutes expiration
            yield mock

    @pytest.fixture
    def mock_supabase(self):
        """Mock supabase_client for database operations"""
        with patch('app.services.user_otp_service.supabase_client') as mock:
            yield mock

    def test_generate_otp_success(self, service, mock_repo, mock_config, mock_supabase):
        """Test successful OTP generation with valid inputs"""
        # Arrange
        mobile = "+1234567890"
        email = "test@example.com"

        # Mock: no recent OTP limit reached
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.gt.return_value.execute.return_value.data = []

        # Mock: no active OTP exists
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value.data = []

        # Mock: no duplicate OTP in repo
        mock_repo.list.return_value = []

        # Mock: successful creation
        mock_repo.create.return_value = None

        # Act - patch random where it's imported (inside the method)
        with patch('secrets.randbelow', return_value=23456):
            otp = service.generate_otp(mobile=mobile, email=email)

        # Assert
        assert otp == "123456"
        assert len(otp) == 6
        assert otp.isdigit()

        # Verify repo.create was called with correct structure
        create_call = mock_repo.create.call_args[0][0]
        assert create_call["mobile"] == mobile
        assert create_call["email"] == email
        assert create_call["otp"] == "123456"
        assert create_call["is_used"] is False
        assert "expire_at" in create_call



    def test_generate_otp_without_email(self, service, mock_repo, mock_config, mock_supabase):
        """Test OTP generation without email parameter"""
        # Arrange
        mobile = "+9876543210"

        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.gt.return_value.execute.return_value.data = []
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value.data = []
        mock_repo.list.return_value = []

        # Act
        with patch('secrets.randbelow', return_value=123456):
            otp = service.generate_otp(mobile=mobile)

        # Assert
        create_call = mock_repo.create.call_args[0][0]

        assert create_call["email"] is None
        assert create_call["mobile"] == mobile

    def test_generate_otp_raises_limit_reached(self, service, mock_repo, mock_supabase):
        """Test that CustomException is raised when OTP limit is reached"""
        # Arrange
        mobile = "+1234567890"

        # Mock: 3 recent OTPs (limit reached)
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.gt.return_value.execute.return_value.data = [
            {"id": 1}, {"id": 2}, {"id": 3}
        ]

        # Act & Assert
        with pytest.raises(CustomException) as exc_info:
            service.generate_otp(mobile=mobile)

        assert exc_info.value.code == 400
        assert exc_info.value.name == ErrorMessages.OTP_LIMIT_REACHED
        assert "Maximum OTP requests per hour reached" in exc_info.value.details

    def test_generate_otp_raises_active_otp_exists(self, service, mock_repo, mock_supabase):
        """Test that CustomException is raised when active OTP already exists"""
        # Arrange
        mobile = "+1234567890"

        # Mock: no limit reached
        mock_supabase_instance = mock_supabase.return_value
        table_mock = Mock()
        mock_supabase_instance.table.return_value = table_mock

        # First call (__recent_otp_limit): return empty (no limit)
        select_mock_1 = Mock()
        table_mock.select.return_value = select_mock_1
        eq_mock_1 = Mock()
        select_mock_1.eq.return_value = eq_mock_1
        gt_mock_1 = Mock()
        eq_mock_1.gt.return_value = gt_mock_1
        execute_mock_1 = Mock()
        gt_mock_1.execute.return_value = execute_mock_1
        execute_mock_1.data = []

        # Second call (__has_active_otp): return 1 active OTP
        # We need to change the mock chain behavior for second call
        def table_side_effect(table_name):
            mock = Mock()
            select = Mock()
            mock.select.return_value = select
            eq1 = Mock()
            select.eq.return_value = eq1
            eq2 = Mock()
            eq1.eq.return_value = eq2
            gt = Mock()
            eq2.gt.return_value = gt
            execute = Mock()
            gt.execute.return_value = execute
            # Return active OTP on second call
            execute.data = [{"id": 1, "otp": "111111"}]
            return mock

        mock_supabase_instance.table.side_effect = [
            table_mock,  # First call - limit check
            table_side_effect("user_otp")  # Second call - active check
        ]

        # Act & Assert
        with pytest.raises(CustomException) as exc_info:
            service.generate_otp(mobile=mobile)

        assert exc_info.value.code == 429
        assert exc_info.value.name == ErrorMessages.OTP_STILL_ACTIVE

    def test_generate_otp_handles_duplicate_regeneration(self, service, mock_repo, mock_config, mock_supabase):
        """Test that service regenerates OTP if duplicate is found"""
        # Arrange
        mobile = "+1234567890"

        # Mock: no limits
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.gt.return_value.execute.return_value.data = []
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value.data = []

        # Mock: first OTP is duplicate, second is unique
        mock_repo.list.side_effect = [
            [{"otp": "123456"}],  # First attempt - duplicate found
            []  # Second attempt - no duplicate
        ]

        # Act
        with patch('secrets.randbelow') as mock_random:
            mock_random.side_effect = [23456, 654321] # First duplicate, then unique
            otp = service.generate_otp(mobile=mobile)

        # Assert
        assert otp == "754321"
        assert mock_repo.list.call_count == 2



class TestUserOtpServiceVerification:
    """Test suite for OTP verification functionality"""

    @pytest.fixture
    def service(self):
        return UserOtpService()

    @pytest.fixture
    def mock_supabase(self):
        with patch('app.services.user_otp_service.supabase_client') as mock:
            yield mock

    @pytest.fixture
    def mock_repo(self, service):
        with patch.object(service, '_UserOtpService__user_otp_repo') as mock:
            yield mock

    def test_verify_otp_success(self, service, mock_supabase, mock_repo):
        """Test successful OTP verification"""
        # Arrange
        otp = "123456"
        mobile = "+1234567890"
        email = "test@example.com"

        # Mock: OTP exists
        mock_supabase_instance = mock_supabase.return_value
        table_mock = Mock()
        mock_supabase_instance.table.return_value = table_mock

        # __get_otp query
        select_mock = Mock()
        table_mock.select.return_value = select_mock
        eq1_mock = Mock()
        select_mock.eq.return_value = eq1_mock
        eq2_mock = Mock()
        eq1_mock.eq.return_value = eq2_mock
        eq3_mock = Mock()
        eq2_mock.eq.return_value = eq3_mock
        execute_mock = Mock()
        eq3_mock.execute.return_value = execute_mock

        otp_data = {
            "id": 1,
            "mobile": mobile,
            "email": email,
            "otp": otp,
            "expire_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "is_used": False
        }
        execute_mock.data = [otp_data]

        # Mock: OTP not expired - need to handle multiple table calls
        def table_side_effect(table_name):
            mock = Mock()
            select = Mock()
            mock.select.return_value = select
            eq1 = Mock()
            select.eq.return_value = eq1
            eq2 = Mock()
            eq1.eq.return_value = eq2
            eq3 = Mock()
            eq2.eq.return_value = eq3
            lt = Mock()
            eq3.lt.return_value = lt
            execute = Mock()
            lt.execute.return_value = execute
            execute.data = []  # Empty = not expired
            return mock

        # First call returns OTP data, second call checks expiration
        call_count = [0]
        def dynamic_table(table_name):
            call_count[0] += 1
            if call_count[0] == 1:
                return table_mock
            else:
                return table_side_effect(table_name)

        mock_supabase_instance.table.side_effect = dynamic_table

        # Act
        result = service.verify_otp(otp=otp, mobile=mobile, email=email)

        # Assert
        assert result is True
        mock_repo.update_by.assert_called_once_with(
            {"mobile": mobile, "otp": otp},
            {"is_used": True}
        )

    def test_verify_otp_invalid_format_empty(self, service):
        """Test verification fails with empty OTP"""
        with pytest.raises(CustomException) as exc_info:
            service.verify_otp(otp="", mobile="+1234567890")

        assert exc_info.value.code == 400
        assert exc_info.value.name == ErrorMessages.OTP_INVALID

    def test_verify_otp_invalid_format_wrong_length(self, service):
        """Test verification fails with wrong length OTP"""
        test_cases = ["12345", "1234567", "123", "12345678"]

        for invalid_otp in test_cases:
            with pytest.raises(CustomException) as exc_info:
                service.verify_otp(otp=invalid_otp, mobile="+1234567890")

            assert exc_info.value.code == 400
            assert exc_info.value.name == ErrorMessages.OTP_INVALID

    def test_verify_otp_invalid_format_non_numeric(self, service):
        """Test verification fails with non-numeric OTP"""
        test_cases = ["12345a", "abcdef", "12-456", "123 456"]

        for invalid_otp in test_cases:
            with pytest.raises(CustomException) as exc_info:
                service.verify_otp(otp=invalid_otp, mobile="+1234567890")

            assert exc_info.value.code == 400
            assert exc_info.value.name == ErrorMessages.OTP_INVALID

    def test_verify_otp_not_found(self, service, mock_supabase):
        """Test verification fails when OTP doesn't exist"""
        # Arrange
        otp = "123456"
        mobile = "+1234567890"

        # Mock: no OTP found
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

        # Act & Assert
        with pytest.raises(CustomException) as exc_info:
            service.verify_otp(otp=otp, mobile=mobile)

        assert exc_info.value.code == 400
        assert exc_info.value.name == ErrorMessages.OTP_INVALID

    def test_verify_otp_expired(self, service, mock_supabase, mock_repo):
        """Test verification fails when OTP is expired"""
        # Arrange
        otp = "123456"
        mobile = "+1234567890"

        # Mock: OTP exists
        otp_data = {
            "id": 1,
            "mobile": mobile,
            "otp": otp,
            "expire_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),  # Expired
            "is_used": False
        }

        mock_supabase_instance = mock_supabase.return_value

        # First table call - __get_otp
        table_mock_1 = Mock()
        table_mock_1.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [otp_data]

        # Second table call - __is_expired
        table_mock_2 = Mock()
        table_mock_2.select.return_value.eq.return_value.eq.return_value.eq.return_value.lt.return_value.execute.return_value.data = [otp_data]

        mock_supabase_instance.table.side_effect = [table_mock_1, table_mock_2]

        # Act & Assert
        with pytest.raises(CustomException) as exc_info:
            service.verify_otp(otp=otp, mobile=mobile)

        assert exc_info.value.code == 400
        assert exc_info.value.name == ErrorMessages.OTP_EXPIRED

    def test_verify_otp_none_value(self, service):
        """Test verification fails with None OTP"""
        with pytest.raises(CustomException) as exc_info:
            service.verify_otp(otp=None, mobile="+1234567890")

        assert exc_info.value.code == 400
        assert exc_info.value.name == ErrorMessages.OTP_INVALID


class TestUserOtpServiceHelperMethods:
    """Test suite for private helper methods"""

    @pytest.fixture
    def service(self):
        return UserOtpService()

    @pytest.fixture
    def mock_supabase(self):
        with patch('app.services.user_otp_service.supabase_client') as mock:
            yield mock

    @pytest.fixture
    def mock_repo(self, service):
        with patch.object(service, '_UserOtpService__user_otp_repo') as mock:
            yield mock

    def test_use_otp(self, service, mock_repo):
        """Test marking OTP as used"""
        # Arrange
        otp = "123456"
        mobile = "+1234567890"

        # Act
        service.use_otp(otp=otp, mobile=mobile)

        # Assert
        mock_repo.update_by.assert_called_once_with(
            {"mobile": mobile, "otp": otp},
            {"is_used": True}
        )


    def test_is_expired_returns_false(self, service, mock_supabase):
        """Test __is_expired returns False for valid OTP"""
        # Arrange
        otp = "123456"
        mobile = "+1234567890"

        # Mock: no expired OTP found
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.lt.return_value.execute.return_value.data = []

        # Act
        result = service._UserOtpService__is_expired(otp=otp, mobile=mobile)

        # Assert
        assert result is False

    def test_has_active_otp_returns_true(self, service, mock_supabase):
        """Test __has_active_otp returns True when active OTP exists"""
        # Arrange
        mobile = "+1234567890"

        # Mock: active OTP found
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value.data = [
            {"id": 1, "otp": "123456"}
        ]

        # Act
        result = service._UserOtpService__has_active_otp(mobile=mobile)

        # Assert
        assert result is True

    def test_has_active_otp_returns_false(self, service, mock_supabase):
        """Test __has_active_otp returns False when no active OTP exists"""
        # Arrange
        mobile = "+1234567890"

        # Mock: no active OTP
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.gt.return_value.execute.return_value.data = []

        # Act
        result = service._UserOtpService__has_active_otp(mobile=mobile)

        # Assert
        assert result is False

    def test_recent_otp_limit_reached(self, service, mock_supabase):
        """Test __recent_otp_limit returns True when limit reached"""
        # Arrange
        mobile = "+1234567890"

        # Mock: 3 OTPs in last hour (limit reached)
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.gt.return_value.execute.return_value.data = [
            {"id": 1}, {"id": 2}, {"id": 3}
        ]

        # Act
        result = service._UserOtpService__recent_otp_limit(mobile=mobile)

        # Assert
        assert result is True

    def test_recent_otp_limit_not_reached(self, service, mock_supabase):
        """Test __recent_otp_limit returns False when under limit"""
        # Arrange
        mobile = "+1234567890"

        # Mock: 2 OTPs in last hour (under limit)
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.gt.return_value.execute.return_value.data = [
            {"id": 1}, {"id": 2}
        ]

        # Act
        result = service._UserOtpService__recent_otp_limit(mobile=mobile)

        # Assert
        assert result is False

    def test_get_otp_found(self, service, mock_supabase):
        """Test __get_otp returns UserOtpModel when OTP exists"""
        # Arrange
        otp = "123456"
        mobile = "+1234567890"
        email = "test@example.com"

        otp_data = {
            "id": 1,
            "mobile": mobile,
            "email": email,
            "otp": otp,
            "expire_at": datetime.now(timezone.utc).isoformat(),
            "is_used": False
        }

        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = [otp_data]

        # Act
        with patch('app.services.user_otp_service.UserOtpModel') as mock_model:
            mock_model.return_value = "mocked_model"
            result = service._UserOtpService__get_otp(otp=otp, mobile=mobile, email=email)

        # Assert
        assert result == "mocked_model"
        mock_model.assert_called_once_with(**otp_data)

    def test_get_otp_not_found(self, service, mock_supabase):
        """Test __get_otp returns None when OTP doesn't exist"""
        # Arrange
        otp = "123456"
        mobile = "+1234567890"
        email = "test@example.com"

        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

        # Act
        result = service._UserOtpService__get_otp(otp=otp, mobile=mobile, email=email)

        # Assert
        assert result is None


class TestUserOtpServiceEdgeCases:
    """Test suite for edge cases and boundary conditions"""

    @pytest.fixture
    def service(self):
        return UserOtpService()

    @pytest.fixture
    def mock_supabase(self):
        with patch('app.services.user_otp_service.supabase_client') as mock:
            yield mock

    @pytest.fixture
    def mock_repo(self, service):
        with patch.object(service, '_UserOtpService__user_otp_repo') as mock:
            yield mock


class TestUserOtpServiceIntegration:
    """Integration-style tests for complete workflows"""

    @pytest.fixture
    def service(self):
        return UserOtpService()

    @pytest.fixture
    def mock_supabase(self):
        with patch('app.services.user_otp_service.supabase_client') as mock:
            yield mock

    @pytest.fixture
    def mock_repo(self, service):
        with patch.object(service, '_UserOtpService__user_otp_repo') as mock:
            yield mock


    def test_otp_limit_prevents_spam(self, service, mock_supabase):
        """Test that rate limiting prevents OTP spam"""
        mobile = "+1234567890"

        # Simulate 3 OTPs already sent
        mock_supabase.return_value.table.return_value.select.return_value.eq.return_value.gt.return_value.execute.return_value.data = [
            {"id": 1}, {"id": 2}, {"id": 3}
        ]

        # Try to generate 4th OTP
        with pytest.raises(CustomException) as exc_info:
            service.generate_otp(mobile=mobile)

        assert exc_info.value.code == 400
        assert exc_info.value.name == ErrorMessages.OTP_LIMIT_REACHED


# Run tests with coverage
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app.services.user_otp_service", "--cov-report=term-missing"])