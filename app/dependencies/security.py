from datetime import datetime, timezone

import jwt
from fastapi import Header, Security, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from gotrue import AuthResponse
from loguru import logger

from app.config.config import supabase_client
from app.exceptions import CustomException
from app.models.user import UserModel

security = HTTPBearer()


def refresh_token(x_refresh_token: str = Header(..., description="X-Refresh-Token is missing")) -> str:
    if not x_refresh_token:
        raise CustomException(code=401, name="X-Refresh-Token is missing", details="X-Refresh-Token is missing")
    return x_refresh_token


def bearer_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> UserModel:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Bearer Token is required for this operation")
    try:
        decoded_token = jwt.decode(credentials.credentials, options={"verify_signature": False})
        expiry_time = datetime.fromtimestamp(decoded_token['exp'], tz=timezone.utc)
        if expiry_time < datetime.now(tz=timezone.utc):
            raise HTTPException(status_code=401, detail="Bearer Token is expired")
        response: AuthResponse = supabase_client().auth.get_user(jwt=credentials.credentials)
        if response.user.is_anonymous and not response.user.email:
            raise HTTPException(status_code=401, detail="Anonymous user is not allowed")
        return UserModel(id=response.user.id, email=response.user.email,
                         token=credentials.credentials,
                         msisdn=response.user.user_metadata.get("msisdn", None),
                         is_verified=response.user.user_metadata.get("email_verified", False),
                         is_anonymous=response.user.is_anonymous
                         )
    except Exception as ex:
        logger.error(f"Token Introspection Exception: {ex}")
        raise HTTPException(status_code=401, detail="Bearer Token is required for this operation")


def bearer_token_anonymous(credentials: HTTPAuthorizationCredentials = Security(security)) -> UserModel:
    if not credentials or not credentials.credentials:
        raise CustomException(code=401, name="Token is required", details="Bearer Token is required for this operation")
    try:
        response: AuthResponse = supabase_client().auth.get_user(jwt=credentials.credentials)
        metadata = response.user.user_metadata
        return UserModel(
            id=response.user.id if metadata.get("user_id", None) is None else metadata.get("user_id", None),
            email=metadata.get("email") if not response.user.email else response.user.email,
            token=credentials.credentials,
            msisdn=response.user.user_metadata.get("msisdn", None),
            is_verified=response.user.user_metadata.get("email_verified", False),
            is_anonymous=response.user.is_anonymous,
            anonymous_user_id=response.user.id
        )
    except Exception as ex:
        logger.error(f"Token Introspection Exception: {ex}")
        raise HTTPException(status_code=401, detail="Bearer Token is required for this operation")


def optional_bearer_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> UserModel | None:
    if not credentials or not credentials.credentials:
        return None
    try:
        response = supabase_client().auth.get_user(jwt=credentials.credentials)

        return UserModel(id=response.user.id, email=response.user.email,
                         token=credentials.credentials,
                         msisdn=response.user.user_metadata.get("msisdn", None),
                         is_verified=response.user.user_metadata.get("email_verified", False))
    except Exception as ex:
        return None


def get_user_from_token(jwt_token: str) -> UserModel | None:
    if not jwt_token:
        return None
    jwt_token = jwt_token.replace("Bearer ", "").replace("bearer ", "")
    try:
        response = supabase_client().auth.get_user(jwt=jwt_token)
        return UserModel(id=response.user.id, email=response.user.email,
                         token=jwt_token,
                         msisdn=response.user.user_metadata.get("msisdn", None),
                         is_verified=response.user.user_metadata.get("email_verified", False))
    except Exception as ex:
        return None


def device_token(x_device_id: str = Header(..., description="X-Device-ID is missing")) -> str:
    if not x_device_id:
        raise CustomException(code=400, name="X-Device-ID is missing", details="X-Device-ID is missing")
    return x_device_id
