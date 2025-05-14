import os
from typing import Optional

from pydantic import BaseModel, EmailStr, ConfigDict, field_validator, ValidationError


class LoginRequest(BaseModel):
    email: EmailStr

    @field_validator("email", mode="before")
    def extract_email(cls, value):
        local_part = value.split('@')[0]
        if "+" in local_part:
            raise ValidationError(f"Invalid email: {local_part}")
        return value


class VerifyOtpRequest(BaseModel):
    user_email: EmailStr
    verification_pin: str


class SignupRequest(BaseModel):
    email: str
    first_name: str
    last_name: str


class ForgotPasswordRequest(BaseModel):
    email: str


class UserInfo(BaseModel):
    is_verified: bool
    referral_code: Optional[str] = None
    should_notify: Optional[bool] = False
    user_token: Optional[str] = None
    role_name: Optional[str] = "User"
    balance: Optional[float] = 0
    currency_code: Optional[str] = os.getenv("DEFAULT_CURRENCY")
    is_newsletter_subscribed: Optional[bool] = False
    msisdn: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language: Optional[str] = "En"
    country: Optional[str] = None
    country_code: Optional[str] = None
    email: EmailStr

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class AuthResponseDTO(BaseModel):
    access_token: str
    refresh_token: str
    user_info: UserInfo
    user_token: Optional[str] = None
    is_verified: bool


class UpdateUserInfoRequest(BaseModel):
    msisdn: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    should_notify: Optional[bool] = False
