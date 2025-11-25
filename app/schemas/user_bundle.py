from typing import Optional

from pydantic import BaseModel


class UserProfileBundle(BaseModel):
    iccid: str
    bundle_code: str
    bundle_name: str
    bundle_type: str
    data_amount: str
    validity: str
    price: float
    currency: str
    is_active: bool
    is_expired: bool
    activated_at: Optional[str] = None
    expired_at: Optional[str] = None

    class Config:
        from_attributes = True


class CallBackNotificationInfo(BaseModel):
    user_id: str
    user_display_name: str
    bundle_display_name: str
    iccid: str
    validity: str
    label: Optional[str] = None
    smdp_address: str
    activation_code: str
    allow_topup: bool
    esim_hub_order_id: str
    searched_countries: Optional[str] = None
    bundle: Optional[UserProfileBundle] = None
