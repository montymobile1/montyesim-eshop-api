"""Admins page DTOs (TEOS-307): the admin accounts, password reset link, revoke admin access."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.admin.common import AdminPage, AdminReason

AdminRoleFilter = Literal["all", "super_admin", "admin"]


class AdminAccountRow(BaseModel):
    id: str
    email: Optional[str] = None
    role: str
    created_at: Optional[datetime] = None
    #: the last ADMIN PORTAL sign-in (``admin_devices``), not Supabase's account-wide one; null = never
    last_sign_in_at: Optional[datetime] = None
    #: the signed-in super admin's own row: its actions are refused (409 ``cannot_target_self``)
    is_current_user: bool = False


class AdminAccountPage(AdminPage[AdminAccountRow]):
    """The Admins list, plus whether "Send reset link" can work on this client (SMTP and the reset URL set)."""
    password_reset_available: bool = False


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
