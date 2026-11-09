"""Admin authentication DTOs."""
from typing import Annotated, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from app.schemas.admin.trusted_devices import AdminDeviceInfo

#: TEOS-307 password rules, the same in the portal (yup): 12+ characters, at most 72 bytes (bcrypt, which
#: Supabase Auth uses, ignores anything after 72 bytes), an upper-case letter, a lower-case letter, a digit
#: and a symbol, and no space at the start or the end.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_BYTES = 72


def check_password_rules(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(f"must be at most {PASSWORD_MAX_BYTES} bytes")
    if value != value.strip():
        raise ValueError("must not start or end with a space")
    if not any(c.isupper() for c in value):
        raise ValueError("must contain an upper-case letter")
    if not any(c.islower() for c in value):
        raise ValueError("must contain a lower-case letter")
    if not any(c.isdigit() for c in value):
        raise ValueError("must contain a digit")
    if not any(not c.isalnum() and not c.isspace() for c in value):
        raise ValueError("must contain a symbol")
    return value


AdminNewPassword = Annotated[str, Field(..., max_length=256), AfterValidator(check_password_rules)]


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=256)
    device: AdminDeviceInfo  # TEOS-255: the browser, to recognise a trusted device

    model_config = ConfigDict(extra="forbid")


class AdminRefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, max_length=4096)
    device: AdminDeviceInfo  # TEOS-255: must be the browser of the session's device row

    model_config = ConfigDict(extra="forbid")


class AdminChangePasswordRequest(BaseModel):
    """Body of ``POST /auth/change-password`` (TEOS-307). The confirmation is checked by the portal."""
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: AdminNewPassword

    model_config = ConfigDict(extra="forbid")


class AdminResetPasswordRequest(BaseModel):
    """Body of ``POST /auth/reset-password`` (TEOS-307): the recovery token of the reset email link (the portal
    reads it from ``#token=``), and the new password."""
    token: str = Field(..., min_length=1, max_length=512)
    new_password: AdminNewPassword

    model_config = ConfigDict(extra="forbid")


class AdminProfile(BaseModel):
    """What the portal needs to know about the signed-in admin."""
    id: str
    email: Optional[str] = None
    role: str
    permissions: List[str]


class AdminSession(BaseModel):
    status: Literal["signed_in"] = "signed_in"  # TEOS-255: vs AdminLoginVerification "verification_required"
    access_token: str
    refresh_token: str
    expires_in: int
    user: AdminProfile
    device_id: Optional[str] = None  # TEOS-255: the admin_devices row of this browser


class AdminPasswordChanged(AdminSession):
    """The new session of the caller (the old one is signed out with the others) and whether the other
    sessions could be signed out."""
    other_sessions_signed_out: bool


class AdminPasswordReset(BaseModel):
    sessions_signed_out: bool
