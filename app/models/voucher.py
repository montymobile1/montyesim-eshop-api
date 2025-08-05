from typing import Optional

from pydantic import BaseModel, Field


class VoucherModel(BaseModel):
    id: int = Field(None, alias="id")
    code: str = Field(None, alias="code")
    amount: float = Field(None, alias="amount")
    is_used: bool = Field(None, alias="is_used")
    used_by: Optional[str] = Field(None, alias="used_by")
    is_active: bool = Field(None, alias="is_active")
    created_at: str = Field(None, alias="created_at")
    updated_at: str = Field(None, alias="updated_at")
    expired_at: Optional[str] = Field(None, alias="expired_at")
