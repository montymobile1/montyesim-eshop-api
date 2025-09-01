from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.dependencies.language import accept_language_header
from app.dependencies.security import refresh_token, bearer_token, device_token
from app.models.user import UserModel
from app.schemas.auth import LoginRequest, VerifyOtpRequest, AuthResponseDTO, UpdateUserInfoRequest
from app.schemas.response import Response
from app.services.auth_service import AuthService

router = APIRouter()

service = AuthService()


@router.post("/login", response_model=Response, dependencies=[Depends(device_token), Depends(accept_language_header)])
async def login(login_request: LoginRequest) -> Response[None]:
    return await service.login(login_request)


@router.post("/tmp-login", response_model=Response[AuthResponseDTO], dependencies=[Depends(device_token)])
async def temporary_login(login_request: LoginRequest) -> Response[AuthResponseDTO]:
    return await service.temporary_login(login_request)


@router.get("/validate-token", response_model=Response[bool],
            dependencies=[Depends(device_token)])
async def validate_token(request: Request) -> Response[bool]:
    return service.validate_token(request)


@router.get("/user-info", response_model=Response[AuthResponseDTO], dependencies=[Depends(bearer_token)])
async def get_user_info(user: Annotated[UserModel, Depends(bearer_token)]) -> Response[AuthResponseDTO]:
    return await service.get_user_info(user=user)


@router.post("/user-info", response_model=Response[AuthResponseDTO],
             dependencies=[Depends(bearer_token), Depends(device_token)])
async def update_user_info(update_request: UpdateUserInfoRequest, user: Annotated[UserModel, Depends(bearer_token)]) -> \
        Response[
            AuthResponseDTO]:
    return await service.update_user_info(user=user, update_request=update_request)


@router.post("/verify_otp", response_model=Response[AuthResponseDTO], dependencies=[Depends(device_token)])
async def verify_otp(verify_request: VerifyOtpRequest) -> Response[AuthResponseDTO]:
    return await service.verify_otp(verify_otp_request=verify_request)


@router.post("/refresh-token", response_model=Response[AuthResponseDTO],
             dependencies=[Depends(refresh_token), Depends(device_token), Depends(accept_language_header)])
async def refresh_token(x_refresh_token: Annotated[str, Depends(refresh_token)]) -> Response[AuthResponseDTO]:
    return await service.refresh_token(x_refresh_token=x_refresh_token)


@router.post("/logout", response_model=Response, dependencies=[Depends(bearer_token), Depends(accept_language_header)])
async def logout(user: Annotated[UserModel, Depends(bearer_token)]) -> Response[None]:
    return service.logout(user)


@router.delete("/delete-account", response_model=Response,
               dependencies=[Depends(bearer_token), Depends(device_token)])
async def delete_account(user: Annotated[UserModel, Depends(bearer_token)]) -> Response[None]:
    return service.delete_account(user)
