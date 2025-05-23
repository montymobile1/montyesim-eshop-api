import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import Request
from loguru import logger

from app.config.config import authenticate, supabase_client, generate_otp, dcb_service_instance
from app.exceptions import CustomException, BadRequestException
from app.models.user import UserModel
from app.repo.device_repo import DeviceRepo
from app.repo.user_order_repo import UserRepo
from app.schemas.auth import LoginRequest, VerifyOtpRequest, UpdateUserInfoRequest, AuthResponseDTO
from app.schemas.dto_mapper import DtoMapper
from app.schemas.response import ResponseHelper, Response
from app.schemas.user_wallet import UserWalletRequestDto, UserWalletResponse
from app.services.promotion_service import PromotionService
from app.services.user_wallet_service import UserWalletService


class AuthService:

    def __init__(self):
        self.__device_repo = DeviceRepo()
        self.__user_repo = UserRepo()
        self.__user_wallet_service = UserWalletService()
        self.__promotion_service = PromotionService()
        self.__dcb_service = dcb_service_instance()

    async def login(self, login_request: LoginRequest) -> Response[None]:
        try:
            if login_request.email:
                return await self.__handle_email_login(login_request=login_request)
            elif login_request.phone:
                return await self.__handle_phone_login(login_request=login_request)
            else:
                raise BadRequestException("Email or Phone are required.")

        except Exception as e:
            logger.error(f"exception on login: {e}")
            raise CustomException(code=400, name="Login Failed", details=str(e))

    async def temporary_login(self, login_request: LoginRequest, x_device_id: str) -> Response[AuthResponseDTO]:
        try:
            if login_request.email:
                user_email = f"{login_request.phone}_esim@gmail.com"
            elif login_request.phone:
                user_email = login_request.email
            else:
                raise BadRequestException("Email or Phone are required.")

            user = self.__user_repo.get_first_by(where={"email": user_email})
            response = supabase_client().auth.sign_in_anonymously(
                {
                    "options": {
                        "data": {
                            "email": login_request.email,
                            "user_id": None if user is None else user.id,
                            "device_id": x_device_id,
                            "msisdn": "",
                            "should_notify": False,
                        }
                    }
                }
            )
            return ResponseHelper.success_data_response(DtoMapper.to_auth_response(supabase_response=response), 0)
        except Exception as e:
            logger.error(f"Exception on temporary login: {e}")
            raise CustomException(code=400, name="Temporary Login Failed", details=str(e))

    async def create_wallet_if_not_exists(self, user_id: str) -> UserWalletResponse | None:
        user_wallet = await self.__user_wallet_service.get_user_wallet_by_user_id(user_id)
        if not user_wallet:
            user_wallet_request_dto = UserWalletRequestDto(
                user_id=user_id,
                amount=0.0,
                currency=os.getenv("DEFAULT_CURRENCY", "USD")
            )
            wallet = await self.__user_wallet_service.create_wallet(user_wallet_request_dto)
            if wallet:
                return wallet
            else:
                return None
        return user_wallet

    async def validate_token(self, request: Request) -> Response[bool]:
        try:
            authorization: str = request.headers.get("Authorization")

            if not authorization or not authorization.startswith("Bearer "):
                return ResponseHelper.success_data_response(False, 0)
            token = authorization.split("Bearer ")[1]
            response = supabase_client().auth.get_user(token)
            return ResponseHelper.success_data_response(True, 0)
        except Exception as e:
            return ResponseHelper.success_data_response(False, 0)

    async def verify_otp(self, verify_otp_request: VerifyOtpRequest, device_id: str) -> Response[AuthResponseDTO]:
        try:
            if verify_otp_request.user_email:
                return await self.__handle_email_otp_verify(verify_otp_request=verify_otp_request, device_id=device_id)
            elif verify_otp_request.phone:
                return await self.__handle_phone_otp_verify(verify_otp_request=verify_otp_request, device_id=device_id)
            else:
                raise BadRequestException("Phone or Email are required.")

        except Exception as e:
            logger.error(f"exception on verify otp: {e}")
            raise CustomException(code=400, name="Verify Failed", details=str(e))

    async def logout(self, user: UserModel, device_id: str) -> Response[None]:
        try:
            supabase_client().auth.sign_out(options={
                "scope": "global",
                "jwt": user.token,
            })
            self.__device_repo.upsert({
                "is_logged_in": False,
                "user_id": user.id,
                "device_id": device_id,
                "timestamp_logout": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
            }, "device_id,user_id")
        except Exception as e:
            logger.error(f"exception on logout: {e}")
            pass
        return ResponseHelper.success_response()

    async def delete_account(self, user: UserModel) -> Response[None]:
        try:
            supabase_client().auth.admin.delete_user(id=user.id)
            return ResponseHelper.success_response()
        except Exception as e:
            raise CustomException(code=400, name="Delete Account Failed", details=str(e))

    async def get_user_info(self, user: UserModel):
        try:
            response = supabase_client().auth.get_user(user.token)
            user_wallet = await self.create_wallet_if_not_exists(user.id)
            return ResponseHelper.success_data_response(
                DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet), 0)
        except Exception as e:
            logger.error(f"exception on get user info: {e}")
            raise CustomException(code=400, name="Get User Info Failed", details=str(e))

    async def update_user_info(self, user: UserModel, update_request: UpdateUserInfoRequest):
        try:
            response = supabase_client().auth.admin.update_user_by_id(user.id, {
                'user_metadata': {
                    'display_email': update_request.email,
                    'first_name': update_request.first_name,
                    'last_name': update_request.last_name,
                    'msisdn': update_request.msisdn,
                    'should_notify': update_request.should_notify,
                    'email': update_request.email,
                }
            })
            user_wallet = await self.create_wallet_if_not_exists(user_id=user.id)
            return ResponseHelper.success_data_response(
                DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet), 0)
        except Exception as e:
            logger.error(f"exception on user info: {e}")
            raise CustomException(code=400, name="User Failed", details=str(e))

    async def refresh_token(self, x_refresh_token: str):
        logger.info(f"received refresh token request: {x_refresh_token}")
        try:
            response = supabase_client().auth.refresh_session(refresh_token=x_refresh_token)
            user_wallet = await self.create_wallet_if_not_exists(user_id=response.user.id)
            return ResponseHelper.success_data_response(
                DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet), 0)
        except Exception as e:
            logger.error(f"exception on refresh token: {e}")
            raise CustomException(code=401, name="Refresh Token Failed", details=str(e))

    def __generate_referral_code(self):
        code = uuid.uuid4().hex[:8].upper()
        while self.__user_repo.get_first_by(where={}, filters={"metadata ->> 'referral_code' ": code}) is not None:
            code = uuid.uuid4().hex[:8].upper()
        return code

    async def __handle_email_login(self, login_request: LoginRequest) -> Response[None]:
        user_exists: UserModel = self.__user_repo.get_first_by(
            where={"email": login_request.email})
        if login_request.email == "test.apple@example.com":
            if not user_exists:
                supabase_client().auth.sign_up({
                    "email": login_request.email,
                    "password": "esim_oss@2025"
                })
            return ResponseHelper.success_response()

        referral_code = self.__generate_referral_code()
        logger.info(f"login request received: {login_request}")
        # if user exists do normal login
        if user_exists:
            authenticate(email=str(login_request.email), referral_code=referral_code)
            return ResponseHelper.success_response()
        else:
            # if user does not exist we check for previous anonymous user and update it
            user = self.__user_repo.get_first_by(where={"email": login_request.email}, filters={
                "metadata->>email": login_request.email})  # db_anonymous_user(login_request.email)
            if user:
                supabase_client().auth.admin.update_user_by_id(uid=user["id"], attributes={
                    "email": login_request.email,
                })
            authenticate(email=str(login_request.email), referral_code=referral_code)
            return ResponseHelper.success_response()

    async def __handle_phone_login(self, login_request: LoginRequest) -> Response[None]:
        user_email = f"{login_request.phone}_esim@gmail.com"
        user_exists: UserModel = self.__user_repo.get_first_by(where={"email": user_email})
        otp = generate_otp()
        if user_exists:
            logger.info(f"generating new otp for user: {user_email}")
            supabase_client().auth.admin.update_user_by_id(uid=user_exists.id, attributes={
                'user_metadata': {
                    "otp": otp,
                }
            })
            await self.__dcb_service.send_sms_template(msisdn=login_request.phone, message=otp)
            return ResponseHelper.success_response()
        user = supabase_client().auth.sign_up({
            "email": user_email,
            "password": f"static_password_{login_request.phone}",
            "options": {
                "data": {
                    "otp": otp,
                    "msisdn": login_request.phone,
                    "email": "",
                }
            }
        })
        logging.info(f"created new user: {user}")
        await self.__dcb_service.send_sms_template(msisdn=login_request.phone, message=otp)
        return ResponseHelper.success_response()

    async def __handle_email_otp_verify(self, verify_otp_request: VerifyOtpRequest, device_id: str) -> Response[
        AuthResponseDTO]:
        logger.info(f"verify_otp email otp request received: {verify_otp_request}")
        if verify_otp_request.user_email == "test.apple@example.com" and verify_otp_request.verification_pin == "123123":
            response = supabase_client().auth.sign_in_with_password({
                "email": verify_otp_request.user_email,
                "password": "esim_oss@2025"
            })
            return ResponseHelper.success_data_response(DtoMapper.to_auth_response(response), 0)
        response = supabase_client().auth.verify_otp(
            {
                "email": verify_otp_request.user_email,
                "token": str(verify_otp_request.verification_pin),
                "type": "email"
            }
        )
        # update device to be logged in

        self.__device_repo.upsert({
            "is_logged_in": True,
            "user_id": response.user.id,
            "device_id": device_id,
            "timestamp_login": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        }, "device_id,user_id")
        user_wallet = await self.create_wallet_if_not_exists(response.user.id)
        return ResponseHelper.success_data_response(
            DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet), 0)

    async def __handle_phone_otp_verify(self, verify_otp_request: VerifyOtpRequest, device_id: str) -> Response[
        AuthResponseDTO]:
        logger.info(f"verify_otp phone request received: {verify_otp_request}")
        user_email = f"{verify_otp_request.phone}_esim@gmail.com"
        user = self.__user_repo.get_first_by(where={"email": user_email})
        if not user:
            raise BadRequestException(f"user {verify_otp_request.phone} not found")
        otp = user.metadata.get("otp", None)
        if otp != verify_otp_request.verification_pin:
            raise BadRequestException(f"Invalid OTP provided")
        response = supabase_client().auth.sign_in_with_password({
            "email": user_email,
            "password": f"static_password_{verify_otp_request.phone}",
        })

        self.__device_repo.upsert({
            "is_logged_in": True,
            "user_id": response.user.id,
            "device_id": device_id,
            "timestamp_login": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        }, "device_id,user_id")
        user_wallet = await self.create_wallet_if_not_exists(response.user.id)
        return ResponseHelper.success_data_response(
            DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet), 0)
