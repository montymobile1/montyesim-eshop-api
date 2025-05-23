from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator, ConfigDict

from ..models.app import DeviceBase


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
