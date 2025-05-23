from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, EmailStr, field_validator, ConfigDict

from app.config.db import PaymentTypeEnum
from app.models.user import UserBundleType
from app.schemas.home import BundleCategoryDTO, CountryDTO, BundleDTO


class CountryRequestDto(BaseModel):
    iso3_code: str
    country_name: str


class RegionRequestDto(BaseModel):
    iso_code: str
    region_name: str


class RelatedSearchRequestDto(BaseModel):
    region: Optional[RegionRequestDto] = None
    countries: Optional[List[CountryRequestDto]] = None


class AssignRequest(BaseModel):
    bundle_code: str
    related_search: Optional[RelatedSearchRequestDto]
    promo_code: Optional[str]
    affiliate_code: Optional[str]
    payment_type: Optional[PaymentTypeEnum] = PaymentTypeEnum.CARD
    promo_code: Optional[str]


class VerifyOtpRequestDto(BaseModel):
    otp: str
    order_id: str
    iccid: Optional[str] = None

class AssignTopUpRequest(BaseModel):
    iccid: str
    bundle_code: str
    payment_type: Optional[PaymentTypeEnum] = PaymentTypeEnum.CARD


class PaymentRequest(BaseModel):
    order_id: str
    user_id: str
    device_id: str
    user_email: EmailStr
    bundle_code: str
    amount: int
    currency: str
    description: str
    order_type: str = "assign"


class PaymentIntentResponse(BaseModel):
    publishable_key: Optional[str] = None
    merchant_identifier: Optional[str] = None
    billing_country_code: Optional[str] = None
    payment_intent_client_secret: Optional[str] = None
    customer_id: Optional[str] = None
    customer_ephemeral_key_secret: Optional[str] = None
    test_env: Optional[bool] = True
    merchant_display_name: Optional[str] = None
    stripe_url_scheme: Optional[str] = "stripe"
    order_id: str

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class UpdateBundleLabelRequest(BaseModel):
    label: str


class SearchedCountry(BaseModel):
    alternative_country: Optional[str] = None
    country: str
    country_code: str
    iso3_code: str
    zone_name: str


class BundleMessage(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


class TransactionHistoryResponse(BaseModel):
    user_order_id: str
    iccid: str
    bundle_type: UserBundleType
    plan_started: bool
    bundle_expired: bool
    created_at: Optional[str]
    bundle: Optional[BundleDTO] = None

    @field_validator("created_at", mode="before")
    @classmethod
    def convert_to_timestamp(cls, value):
        if value is None:
            return None
        dt = datetime.fromisoformat(value)
        return str(int(dt.timestamp()))


class EsimBundleResponse(BaseModel):
    is_topup_allowed: bool
    plan_started: bool
    bundle_expired: bool
    label_name: Optional[str] = None
    order_number: str
    order_status: str
    searched_countries: Optional[RelatedSearchRequestDto] | List[str] = None
    qr_code_value: str
    activation_code: str
    smdp_address: str
    validity_date: str
    iccid: str
    payment_date: str
    shared_with: Optional[int] = None
    display_title: str
    display_subtitle: str
    bundle_code: str
    bundle_category: BundleCategoryDTO
    bundle_marketing_name: str
    bundle_name: str
    count_countries: int
    currency_code: str
    gprs_limit_display: str
    price: float
    price_display: str
    unlimited: bool
    validity: int
    validity_display: str
    plan_type: str
    activity_policy: str
    bundle_message: List[BundleMessage]
    countries: List[CountryDTO]
    icon: Optional[str]
    transaction_history: List[TransactionHistoryResponse]

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    @field_validator("payment_date", mode="before")
    @classmethod
    def convert_to_timestamp(cls, value):
        dt = datetime.fromisoformat(value)
        return str(int(dt.timestamp()))


class ConsumptionResponse(BaseModel):
    data_allocated: float
    data_used: float
    data_remaining: float
    data_allocated_display: str
    data_used_display: str
    data_remaining_display: str
    plan_status: str


class PaymentDetailsDTO(BaseModel):
    id: str
    description: str
    payment_method: str
    card_number: str
    receipt_email: str
    address: str
    display_brand: str
    country: str
    card_display: str


class UserOrderHistoryResponse(BaseModel):
    order_number: str
    order_status: str
    order_amount: float
    order_currency: str
    order_display_price: str
    order_date: str
    order_type: str
    quantity: Optional[int] = 1
    company_name: Optional[str] = None
    payment_details: Optional[PaymentDetailsDTO] = None
    payment_type: Optional[PaymentTypeEnum] = PaymentTypeEnum.CARD
    bundle_details: BundleDTO

    @field_validator("order_date", mode="before")
    @classmethod
    def convert_to_timestamp(cls, value):
        dt = datetime.fromisoformat(value)
        return str(int(dt.timestamp()))
