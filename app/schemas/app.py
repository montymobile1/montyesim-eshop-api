from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator, ConfigDict, Field


class DeviceBase(BaseModel):
    fcm_token: Optional[str] = Field(None, alias="fcm_token")
    manufacturer: Optional[str] = Field(None, alias="manufacturer")
    device_model: Optional[str] = Field(None, alias="device_model")
    os: Optional[str] = Field(None, alias="os")
    os_version: Optional[str] = Field(None, alias="os_version")
    app_version: Optional[str] = Field(None, alias="app_version")
    ram_size: Optional[str] = Field(None, alias="ram_size")
    screen_resolution: Optional[str] = Field(None, alias="screen_resolution")
    is_rooted: Optional[bool] = Field(None, alias="is_rooted")


class DeviceRequest(DeviceBase):
    pass


class ContactUsRequest(BaseModel):
    email: EmailStr
    content: str


class DeleteDeviceRequest(BaseModel):
    email: EmailStr


class FaqResponse(BaseModel):
    question: str
    answer: str


class PageContentResponse(BaseModel):
    page_title: str
    page_content: str
    page_intro: str


class UserNotificationResponse(BaseModel):
    notification_id: int
    title: str
    content: str
    datetime: str
    transaction_status: Optional[str] = ""
    transaction: Optional[str] = ""
    transaction_message: Optional[str] = ""
    status: Optional[bool] = False
    iccid: Optional[str] = None
    category: Optional[str] = None
    translated_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    @field_validator("datetime", mode="before")
    @classmethod
    def convert_to_timestamp(cls, value, values):
        dt = datetime.fromisoformat(value)
        return str(int(dt.timestamp()))


class GlobalConfiguration(BaseModel):
    key: str
    value: str


class ExchangeRate(BaseModel):
    system_currency_code: str
    currency_code: str
    current_rate: float
    new_rate: float


class BannerResponse(BaseModel):
    title: str
    description: str
    image: str
    action: str
