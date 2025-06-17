import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.auth_service import AuthService
from app.schemas.auth import LoginRequest, VerifyOtpRequest, UpdateUserInfoRequest
from app.models.user import UserModel
from app.schemas.response import ResponseHelper

@pytest.fixture
def auth_service():
    service = AuthService()
    service._AuthService__user_repo = MagicMock()
    service._AuthService__device_repo = MagicMock()
    service._AuthService__user_wallet_service = AsyncMock()
    service._AuthService__dcb_service = MagicMock()
    return service

@pytest.mark.asyncio
async def test_login_email(auth_service):
    login_request = LoginRequest(email="test@example.com", phone=None)
    with patch("app.services.auth_service.supabase_client"), \
         patch.object(auth_service, "_AuthService__handle_email_login", return_value=ResponseHelper.success_response()) as mock_email_login:
        resp = await auth_service.login(login_request)
        assert resp.status == "success"
        mock_email_login.assert_called_once()

@pytest.mark.asyncio
async def test_login_phone(auth_service):
    login_request = LoginRequest(email=None, phone="123456789")
    with patch("app.services.auth_service.supabase_client"), \
         patch.object(auth_service, "_AuthService__handle_phone_login", return_value=ResponseHelper.success_response()) as mock_phone_login:
        resp = await auth_service.login(login_request)
        assert resp.status == "success"
        mock_phone_login.assert_called_once()

@pytest.mark.asyncio
async def test_login_missing_fields(auth_service):
    login_request = LoginRequest(email=None, phone=None)
    with pytest.raises(Exception):
        await auth_service.login(login_request)

@pytest.mark.asyncio
async def test_validate_token_valid(auth_service):
    request = MagicMock()
    request.headers = {"Authorization": "Bearer validtoken"}
    with patch("app.services.auth_service.supabase_client") as mock_supabase:
        mock_supabase().auth.get_user.return_value = MagicMock()
        resp = await auth_service.validate_token(request)
        assert resp.data is True

@pytest.mark.asyncio
async def test_validate_token_invalid(auth_service):
    request = MagicMock()
    request.headers = {"Authorization": "Bearer invalidtoken"}
    with patch("app.services.auth_service.supabase_client") as mock_supabase:
        mock_supabase().auth.get_user.side_effect = Exception("Invalid token")
        resp = await auth_service.validate_token(request)
        assert resp.data is False

@pytest.mark.asyncio
async def test_logout(auth_service):
    user = UserModel(id="user1", token="token", email="test@example.com", msisdn="123456789", is_verified=True)
    with patch("app.services.auth_service.supabase_client") as mock_supabase:
        mock_supabase().auth.sign_out.return_value = None
        resp = await auth_service.logout(user, "device1")
        assert resp.status == "success"

@pytest.mark.asyncio
async def test_delete_account(auth_service):
    user = UserModel(id="user1", token="token", email="test@example.com", msisdn="123456789", is_verified=True)
    with patch("app.services.auth_service.supabase_client") as mock_supabase:
        mock_supabase().auth.admin.delete_user.return_value = None
        resp = await auth_service.delete_account(user)
        assert resp.status == "success"

@pytest.mark.asyncio
async def test_get_user_info(auth_service):
    user = UserModel(id="user1", token="token", email="test@example.com", msisdn="123456789", is_verified=True)
    with patch("app.services.auth_service.supabase_client") as mock_supabase, \
         patch.object(auth_service, "create_wallet_if_not_exists", AsyncMock(return_value=None)):
        mock_user = MagicMock(id="user1")
        mock_user.user_metadata = {
            "full_name": "Test User",
            "first_name": "Test",
            "last_name": "User",
            "referral_code": "ref123",
            "msisdn": "123456789",
            "email": "test@example.com",
            "display_email": "test@example.com",
            "email_verified": True,
            "should_notify": True
        }
        mock_user.email = "test@example.com"
        mock_session = MagicMock()
        mock_session.access_token = "access_token"
        mock_session.refresh_token = "refresh_token"
        mock_supabase().auth.get_user.return_value = MagicMock(user=mock_user, session=mock_session)
        resp = await auth_service.get_user_info(user, "USD")
        assert resp.status == "success"

@pytest.mark.asyncio
async def test_update_user_info(auth_service):
    user = UserModel(id="user1", token="token", email="test@example.com", msisdn="123456789", is_verified=True)
    update_request = UpdateUserInfoRequest(email="new@example.com", first_name="First", last_name="Last", msisdn="123", should_notify=True)
    with patch("app.services.auth_service.supabase_client") as mock_supabase, \
         patch.object(auth_service, "create_wallet_if_not_exists", AsyncMock(return_value=None)):
        mock_user = MagicMock(id="user1")
        mock_user.user_metadata = {
            "full_name": "Test User",
            "first_name": "Test",
            "last_name": "User",
            "referral_code": "ref123",
            "msisdn": "123456789",
            "email": "test@example.com",
            "display_email": "test@example.com",
            "email_verified": True,
            "should_notify": True
        }
        mock_user.email = "test@example.com"
        mock_session = MagicMock()
        mock_session.access_token = "access_token"
        mock_session.refresh_token = "refresh_token"
        mock_supabase().auth.admin.update_user_by_id.return_value = MagicMock(user=mock_user, session=mock_session)
        resp = await auth_service.update_user_info(user, update_request, "USD")
        assert resp.status == "success"

@pytest.mark.asyncio
async def test_refresh_token(auth_service):
    with patch("app.services.auth_service.supabase_client") as mock_supabase, \
         patch.object(auth_service, "create_wallet_if_not_exists", AsyncMock(return_value=None)):
        mock_user = MagicMock(id="user1")
        mock_user.user_metadata = {
            "full_name": "Test User",
            "first_name": "Test",
            "last_name": "User",
            "referral_code": "ref123",
            "msisdn": "123456789",
            "email": "test@example.com",
            "display_email": "test@example.com",
            "email_verified": True,
            "should_notify": True
        }
        mock_user.email = "test@example.com"
        mock_session = MagicMock()
        mock_session.access_token = "access_token"
        mock_session.refresh_token = "refresh_token"
        mock_supabase().auth.refresh_session.return_value = MagicMock(user=mock_user, session=mock_session)
        resp = await auth_service.refresh_token("refresh_token", "USD")
        assert resp.status == "success"
