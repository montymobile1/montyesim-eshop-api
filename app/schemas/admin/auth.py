"""Admin authentication DTOs."""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=256)

    model_config = ConfigDict(extra="forbid")


class AdminRefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, max_length=4096)

    model_config = ConfigDict(extra="forbid")


class AdminProfile(BaseModel):
    """What the portal needs to know about the signed-in admin."""
    id: str
    email: Optional[str] = None
    role: str
    permissions: List[str]


class AdminSession(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    user: AdminProfile
