import os
import re
from typing import Optional

from pydantic import BaseModel, EmailStr, ConfigDict, field_validator, ValidationError

from app.exceptions import BadRequestException


class LoginRequest(BaseModel):
    email: Optional[EmailStr] = None
    phone: Optional[str] = None

    @field_validator("email", mode="before")
    def extract_email(cls, value):
        if not value:
            return True
        local_part = value.split('@')[0]
        if "+" in local_part:
            raise ValidationError(f"Invalid email: {local_part}")
        return value

    @field_validator("phone", mode="before")
    def extract_phone(cls, value):
        if not value:
            return value
        pattern = os.getenv("DCB_MSISDN_REGEX")
        if not pattern:
            raise BadRequestException("Regex is not set")
        if bool(re.match(re.compile(pattern), value)):
            return value
        raise BadRequestException(f"Invalid phone number")


class VerifyOtpRequest(BaseModel):
    user_email: Optional[EmailStr] = None
    phone: Optional[str] = None
    verification_pin: str

    @field_validator("phone", mode="before")
    def extract_phone(cls, value):
        if not value:
            return value
        pattern = os.getenv("DCB_MSISDN_REGEX")
        if not pattern:
            raise BadRequestException("Regex is not set")
        if bool(re.match(re.compile(pattern), value)):
            return value
        raise BadRequestException(f"Invalid phone number")


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
    email: Optional[EmailStr] = None

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class AuthResponseDTO(BaseModel):
    access_token: str
    refresh_token: str
    user_info: UserInfo
    user_token: Optional[str] = None
    is_verified: bool


class UpdateUserInfoRequest(BaseModel):
    email: Optional[EmailStr] = None
    msisdn: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    should_notify: Optional[bool] = False
    email: Optional[EmailStr] = None
