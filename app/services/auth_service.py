import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import Request
from loguru import logger

from app.config.config import authenticate, supabase_client, generate_otp, dcb_service_instance
from app.config.constants import ErrorMessages
from app.config.context import device_id_context, currency_context
from app.exceptions import CustomException, BadRequestException
from app.models.user import UserModel
from app.repo.device_repo import DeviceRepo
from app.repo.user_order_repo import UserRepo
from app.schemas.auth import LoginRequest, VerifyOtpRequest, UpdateUserInfoRequest, AuthResponseDTO
from app.schemas.dto_mapper import DtoMapper
from app.schemas.response import ResponseHelper, Response
from app.schemas.user_wallet import UserWalletRequestDto, UserWalletResponse
from app.services.user_wallet_service import UserWalletService


class AuthService:

    def __init__(self):
        self.__device_repo = DeviceRepo()
        self.__user_repo = UserRepo()
        self.__user_wallet_service = UserWalletService()
        self.__dcb_service = dcb_service_instance()

    async def login(self, login_request: LoginRequest) -> Response[None]:
        try:
            if login_request.email:
                return self.__handle_email_login(login_request=login_request)
            elif login_request.phone:
                return self.__handle_phone_login(login_request=login_request)
            else:
                raise BadRequestException("Email or Phone are required.")

        except Exception as e:
            logger.error(f"exception on login: {e}")
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED, details=str(e))

    async def temporary_login(self, login_request) -> Response[AuthResponseDTO]:
        try:
            user = self.__user_repo.get_first_by(where={"email": login_request.email})
            response = supabase_client().auth.sign_in_anonymously(
                {
                    "options": {
                        "data": {
                            "email": login_request.email,
                            "user_id": None if user is None else user.id,
                            "device_id": device_id_context.get(),
                            "msisdn": "",
                            "should_notify": False,
                        }
                    }
                }
            )
            return ResponseHelper.success_data_response(DtoMapper.to_auth_response(supabase_response=response), 0)
        except Exception as e:
            logger.error(f"Exception on temporary login: {e}")
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED, details=str(e))

    async def create_wallet_if_not_exists(self, user_id: str, currency_code: str) -> UserWalletResponse | None:
        user_wallet = await self.__user_wallet_service.get_user_wallet_by_user_id(user_id=user_id,
                                                                                  currency_code=currency_code)
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

    def validate_token(self, request: Request) -> Response[bool]:
        try:
            authorization: str = request.headers.get("Authorization")

            if not authorization or not authorization.startswith("Bearer "):
                return ResponseHelper.success_data_response(False, 0)
            token = authorization.split("Bearer ")[1]
            supabase_client().auth.get_user(token)
            return ResponseHelper.success_data_response(True, 0)
        except Exception:
            return ResponseHelper.success_data_response(False, 0)

    async def verify_otp(self, verify_otp_request: VerifyOtpRequest) -> Response[
        AuthResponseDTO]:
        try:
            device_id = device_id_context.get()
            if verify_otp_request.user_email:
                return await self.__handle_email_otp_verify(verify_otp_request=verify_otp_request, device_id=device_id)
            elif verify_otp_request.phone:
                return await self.__handle_phone_otp_verify(verify_otp_request=verify_otp_request, device_id=device_id)
            else:
                raise BadRequestException("Phone or Email are required.")

        except Exception as e:
            logger.error(f"exception on verify otp: {e}")
            raise CustomException(code=400, name=ErrorMessages.VERIFY_FAILED, details=str(e))

    def logout(self, user: UserModel) -> Response[None]:
        device_id = device_id_context.get()
        logger.info(f"logging out user {user} device {device_id}")
        try:
            supabase_client().auth.sign_out(options={
                "scope": "global",
                "jwt": user.token,
            })

        except Exception as e:
            logger.error(f"exception on logout: {e}")
        finally:
            self.__upsert_device(user_id=user.id, device_id=device_id, is_logged_in=False)
        return ResponseHelper.success_response()

    def delete_account(self, user: UserModel) -> Response[None]:
        try:
            supabase_client().auth.admin.delete_user(id=user.id)
            return ResponseHelper.success_response()
        except Exception as e:
            raise CustomException(code=400, name=ErrorMessages.DELETE_ACCOUNT_FAILED, details=str(e))

    async def get_user_info(self, user: UserModel):
        try:
            response = supabase_client().auth.get_user(user.token)
            user_wallet = await self.create_wallet_if_not_exists(user_id=user.id, currency_code=currency_context.get())
            return ResponseHelper.success_data_response(
                DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet,
                                           currency=currency_context.get()),
                0)
        except Exception as e:
            logger.error(f"exception on get user info: {e}")
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED, details=str(e))

    async def update_user_info(self, user: UserModel, update_request: UpdateUserInfoRequest):
        try:
            currency_code = currency_context.get()
            response = supabase_client().auth.admin.update_user_by_id(user.id, {
                'user_metadata': {
                    'display_email': update_request.email,
                    'first_name': update_request.first_name,
                    'last_name': update_request.last_name,
                    'msisdn': update_request.msisdn,
                    'should_notify': update_request.should_notify,
                }
            })
            user_wallet = await self.create_wallet_if_not_exists(user_id=user.id, currency_code=currency_code)
            return ResponseHelper.success_data_response(
                DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet, currency=currency_code),
                0)
        except Exception as e:
            logger.error(f"exception on user info: {e}")
            raise CustomException(code=400, name=ErrorMessages.REQUEST_FAILED, details=str(e))

    async def refresh_token(self, x_refresh_token: str):
        logger.info(f"received refresh token request: {x_refresh_token}")
        currency_code = currency_context.get()
        try:
            response = supabase_client().auth.refresh_session(refresh_token=x_refresh_token)
            user_wallet = await self.create_wallet_if_not_exists(user_id=response.user.id, currency_code=currency_code)
            return ResponseHelper.success_data_response(
                DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet, currency=currency_code),
                0)
        except Exception as e:
            logger.error(f"exception on refresh token: {e}")
            raise CustomException(code=401, name=ErrorMessages.REQUEST_FAILED, details=str(e))

    def __generate_referral_code(self):
        code = uuid.uuid4().hex[:8].upper()
        while self.__user_repo.get_first_by(where={}, filters={"metadata ->> 'referral_code' ": code}) is not None:
            code = uuid.uuid4().hex[:8].upper()
        return code

    def __handle_email_login(self, login_request: LoginRequest) -> Response[None]:
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
        if user_exists:
            authenticate(email=str(login_request.email), referral_code=referral_code)
            return ResponseHelper.success_response()
        else:
            user = self.__user_repo.get_first_by(where={"email": login_request.email}, filters={
                "metadata->>email": login_request.email})
            if user:
                supabase_client().auth.admin.update_user_by_id(uid=user["id"], attributes={
                    "email": login_request.email,
                })
            authenticate(email=str(login_request.email), referral_code=referral_code)
            return ResponseHelper.success_response()

    def __handle_phone_login(self, login_request: LoginRequest) -> Response[None]:
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
            self.__dcb_service.send_otp(otp=otp, msisdn=login_request.phone)
            return ResponseHelper.success_response()
        user = supabase_client().auth.sign_up({
            "email": user_email,
            "password": f"static_password_{login_request.phone}",
            "options": {
                "data": {
                    "otp": otp,
                    "msisdn": login_request.phone
                }
            }
        })
        logging.info(f"created new user: {user}")
        self.__dcb_service.send_otp(otp=otp, msisdn=login_request.phone)
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
        self.__upsert_device(user_id=response.user.id, device_id=device_id, is_logged_in=True)
        user_wallet = await self.create_wallet_if_not_exists(user_id=response.user.id,
                                                             currency_code=os.getenv("DEFAULT_CURRENCY"))
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
            raise BadRequestException(ErrorMessages.INVALID_OTP)
        response = supabase_client().auth.sign_in_with_password({
            "email": user_email,
            "password": f"static_password_{verify_otp_request.phone}",
        })
        self.__upsert_device(user_id=response.user.id, device_id=device_id, is_logged_in=True)
        user_wallet = await self.create_wallet_if_not_exists(user_id=response.user.id,
                                                             currency_code=os.getenv("DEFAULT_CURRENCY"))
        return ResponseHelper.success_data_response(
            DtoMapper.to_auth_response(supabase_response=response, user_wallet=user_wallet), 0)

    def __upsert_device(self, user_id: str, device_id: str, is_logged_in: bool = False):
        self.__device_repo.upsert({
            "is_logged_in": is_logged_in,
            "user_id": user_id,
            "device_id": device_id,
            "timestamp_logout": datetime.now(timezone.utc).strftime(
                '%Y-%m-%d %H:%M:%S.%f') if not is_logged_in else None,
            "timestamp_login": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f') if is_logged_in else None,
        }, "device_id,user_id")
