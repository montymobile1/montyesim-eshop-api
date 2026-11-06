"""Admins page DTOs (TEOS-307): the admin accounts, password reset link, revoke admin access."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.admin.common import AdminReason

AdminRoleFilter = Literal["all", "super_admin", "admin"]


class AdminAccountRow(BaseModel):
    id: str
    email: Optional[str] = None
    role: str
    created_at: Optional[datetime] = None
    last_sign_in_at: Optional[datetime] = None
    #: the signed-in super admin's own row: its actions are refused (409 ``cannot_target_self``)
    is_current_user: bool = False


class AdminRevokeAccess(BaseModel):
    """Body of ``POST /admins/{id}/revoke-access``. ``reason``: 3 to 500 characters after trimming."""
    reason: AdminReason

    model_config = ConfigDict(extra="forbid")


class AdminPasswordResetSent(BaseModel):
    id: str
    email: Optional[str] = None


class AdminAccessRevoked(BaseModel):
    id: str
    email: Optional[str] = None
    previous_role: str
    sessions_signed_out: bool
