import json
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.config.db import UserOrderType, OrderStatusEnum, UserBundleType

class VoucherModel(BaseModel):
    id: int =  Field(None, alias="id")
    code: str =  Field(None, alias="code")
    amount: float =  Field(None, alias="amount")
    is_used: bool =  Field(None, alias="is_used")
    used_by: Optional[str] = Field(None, alias="used_by")
    is_active: bool = Field(None, alias="is_active")
    created_at: str = Field(None, alias="created_at")
    updated_at: str = Field(None, alias="updated_at")
