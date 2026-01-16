from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.types import Scope

from app.exceptions import CustomException
from app.models.user import UserModel, UsersCopyModel
from app.schemas.auth import LoginRequest, UpdateUserInfoRequest, VerifyOtpRequest
from app.schemas.response import ResponseHelper
from app.services.auth_service import AuthService


@pytest.fixture
def auth_service():
    service = AuthService()
    service._AuthService__user_repo = MagicMock()
    service._AuthService__device_repo = MagicMock()
    service._AuthService__user_wallet_service = AsyncMock()
    service._AuthService__dcb_service = MagicMock()
    service._AuthService__user_otp_service = MagicMock()
    return service


@pytest.fixture
def mock_request():
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "client": ("testclient", 5000),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_login_email(auth_service):
    login_request = LoginRequest(email="test@example.com", phone=None)
    with patch("app.services.auth_service.supabase_client"), \
            patch.object(auth_service, "_AuthService__handle_email_login",
                         return_value=ResponseHelper.success_response()) as mock_email_login:
        resp = await auth_service.login(login_request)
        assert resp.status == "success"
        mock_email_login.assert_called_once()


@pytest.mark.asyncio
async def test_login_phone(auth_service):
    login_request = LoginRequest(email=None, phone="+961234567890")  # Fixed: Use valid international phone format
    with patch("app.services.auth_service.supabase_client"), \
            patch.object(auth_service, "_AuthService__handle_phone_login",
                         return_value=ResponseHelper.success_response()) as mock_phone_login:
        resp = await auth_service.login(login_request)
        assert resp.status == "success"
        mock_phone_login.assert_called_once()


@pytest.mark.asyncio
async def test_login_missing_fields(auth_service):
    login_request = LoginRequest(email=None, phone=None)
    with pytest.raises(Exception):
        await auth_service.login(login_request)


@pytest.mark.asyncio
async def test_temporary_login(auth_service):
    login_request = LoginRequest(email="test@example.com", phone=None)
    auth_service._AuthService__user_repo.get_first_by.return_value = None

    with patch("app.services.auth_service.supabase_client") as mock_supabase, \
         patch("app.schemas.dto_mapper.DtoMapper.to_auth_response") as mock_dto_mapper:

        mock_auth = MagicMock()
        mock_user = MagicMock()
        mock_user.id = "user123"
        mock_user.user_metadata = {
            "email": "test@example.com",
            "first_name": "Test",
            "last_name": "User",
            "referral_code": "REF123",
            "msisdn": "",
            "should_notify": False,
            "email_verified": True
        }
        mock_user.email = "test@example.com"
        mock_session = MagicMock()
        mock_session.access_token = "token123"

        mock_auth.sign_in_anonymously.return_value = MagicMock(user=mock_user, session=mock_session)
        mock_supabase.return_value.auth = mock_auth
        mock_dto_mapper.return_value = MagicMock()

        response = await auth_service.temporary_login(login_request, "device123")
        assert response.status == "success"


@pytest.mark.asyncio
async def test_create_wallet_if_not_exists(auth_service):
    auth_service._AuthService__user_wallet_service.get_user_wallet.return_value = None
    auth_service._AuthService__user_wallet_service.create_user_wallet.return_value = MagicMock()

    result = await auth_service.create_wallet_if_not_exists("user123", "USD")
    assert result is not None


def test_validate_token_valid(auth_service, mock_request):
    with patch("app.services.auth_service.authenticate") as mock_auth:
        mock_auth.return_value = True
        response = auth_service.validate_token(mock_request)
        assert response.status == "success"


def test_validate_token_invalid(auth_service, mock_request):
    with patch("app.services.auth_service.authenticate") as mock_auth:
        mock_auth.return_value = False
        response = auth_service.validate_token(mock_request)
        assert response.status == "success"  # Fixed: The service returns success even for invalid tokens based on implementation
        assert response.data == False  # The actual validation result is in data


@pytest.mark.asyncio
async def test_verify_otp_phone(auth_service):
    verify_request = VerifyOtpRequest(otp="123456", phone="+961234567890", user_email=None, verification_pin="123456")  # Added required field
    with patch.object(auth_service, "_AuthService__handle_phone_otp_verify",
                      return_value=ResponseHelper.success_response()) as mock_verify:
        response = await auth_service.verify_otp(verify_request, "device123")
        assert response.status == "success"
        mock_verify.assert_called_once()


@pytest.mark.asyncio
async def test_verify_otp_email(auth_service):
    verify_request = VerifyOtpRequest(otp="123456", phone=None, user_email="test@example.com", verification_pin="123456")  # Added required field
    with patch.object(auth_service, "_AuthService__handle_email_otp_verify",
                      return_value=ResponseHelper.success_response()) as mock_verify:
        response = await auth_service.verify_otp(verify_request, "device123")
        assert response.status == "success"
        mock_verify.assert_called_once()


@pytest.mark.asyncio
async def test_get_user_info(auth_service):
    user = UserModel(id="123", email="test@example.com", token="token", msisdn="", is_verified=True)
    auth_service._AuthService__user_wallet_service.get_user_wallet.return_value = MagicMock()

    with patch("app.services.auth_service.supabase_client") as mock_supabase, \
         patch("app.schemas.dto_mapper.DtoMapper.to_auth_response") as mock_dto_mapper:

        mock_supabase.return_value.auth.get_user.return_value = MagicMock()
        mock_dto_mapper.return_value = MagicMock()

        response = await auth_service.get_user_info(user, "USD")
        assert response.status == "success"


@pytest.mark.asyncio
async def _run_update_user_info_test(auth_service, login_type, expected_metadata):
    user = UserModel(id="123", email="test@example.com", token="token", msisdn="", is_verified=True)
    update_request = UpdateUserInfoRequest(email="user@email.com", msisdn="123456789", first_name="John", last_name="Doe", should_notify=True)
    auth_service._AuthService__user_repo.update_by.return_value = []
    auth_service._AuthService__user_repo.get_first_by.return_value = UsersCopyModel(id="123", email="user@gmail.com", metadata={"login_type": login_type})
    auth_service._AuthService__user_wallet_service.get_user_wallet.return_value = MagicMock()
    with patch("app.services.auth_service.supabase_client") as mock_supabase, \
         patch("app.services.auth_service.get_config") as mock_get_config, \
         patch("app.schemas.dto_mapper.DtoMapper.to_auth_response") as mock_dto_mapper, \
         patch.object(auth_service, "create_wallet_if_not_exists", new_callable=AsyncMock) as mock_create_wallet:
        mock_get_config.return_value = login_type
        mock_update_user = MagicMock()
        mock_admin = MagicMock()
        mock_admin.update_user_by_id = mock_update_user
        mock_auth = MagicMock()
        mock_auth.admin = mock_admin
        mock_supabase.return_value.auth = mock_auth
        mock_dto_mapper.return_value = MagicMock()
        mock_create_wallet.return_value = MagicMock()
        response = await auth_service.update_user_info(user, update_request, "USD")
        assert response.status == "success"
        mock_update_user.assert_called_once_with(
            user.id,
            {'user_metadata': expected_metadata}
        )


@pytest.mark.asyncio
async def test_update_user_info_email(auth_service):
    expected_metadata = {
        'first_name': 'John',
        'last_name': 'Doe',
        'msisdn': '123456789',
        'should_notify': True
    }
    await _run_update_user_info_test(auth_service, 'email', expected_metadata)


@pytest.mark.asyncio
async def test_update_user_info_phone(auth_service):
    expected_metadata = {
        'first_name': 'John',
        'last_name': 'Doe',
        'should_notify': True
    }
    await _run_update_user_info_test(auth_service, 'phone', expected_metadata)


@pytest.mark.asyncio
async def test_update_user_info_email_phone(auth_service):
    expected_metadata = {
        'first_name': 'John',
        'last_name': 'Doe',
        'should_notify': True
    }
    await _run_update_user_info_test(auth_service, 'phone', expected_metadata)


@pytest.mark.asyncio
async def test_refresh_token(auth_service):
    with patch("app.services.auth_service.supabase_client") as mock_supabase, \
         patch("app.schemas.dto_mapper.DtoMapper.to_auth_response") as mock_dto_mapper:

        mock_user = MagicMock()
        mock_user.id = "user123"
        mock_user.email = "test@example.com"
        mock_user.user_metadata = {
            "email": "test@example.com",
            "first_name": "Test",
            "last_name": "User",
            "referral_code": "REF123",
            "msisdn": "",
            "should_notify": False
        }

        mock_auth = MagicMock()
        mock_auth.refresh_session.return_value = MagicMock(
            user=mock_user,
            session=MagicMock(access_token="new_token", refresh_token="new_refresh")
        )
        mock_supabase.return_value.auth = mock_auth
        auth_service._AuthService__user_wallet_service.get_user_wallet.return_value = MagicMock()
        mock_dto_mapper.return_value = MagicMock()

        response = await auth_service.refresh_token("refresh_token", "USD")
        assert response.status == "success"
