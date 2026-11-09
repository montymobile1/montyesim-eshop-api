"""Admin trusted devices DTOs (TEOS-255 / TEOS-256 / TEOS-310).

The contract is described in adminDocs ``TEOS-255-256-api-contract.md``. A device is a browser: the portal
sends the raw browser values plus a random ``device_key`` it keeps in the browser; the API hashes them
(``app/services/admin/device_hash.py``), so the browser never sends a ready-made hash.
"""
import re
from datetime import datetime
from typing import Annotated, List, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _device_key(value: str) -> str:
    if not _UUID.match(value or ""):
        raise ValueError("must be a UUID")
    return value.lower()


def _device_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(value.split())
    if not value:
        raise ValueError("must not be empty")
    if len(value) > 60:
        raise ValueError("must be at most 60 characters")
    return value


Text = Annotated[str, Field(max_length=300)]
DeviceName = Annotated[Optional[str], Field(default=None, max_length=200), AfterValidator(_device_name)]


class AdminDeviceInfo(BaseModel):
    """What the portal reads from the browser (``src/core/helpers/deviceInfo.js``).

    Hashed (stable across sign-ins): device_key, browser, os, cpu_cores, memory_gb, touch_points, gpu,
    language. Display only (they change on their own): browser_version, os_version, timezone, screen,
    color_depth. Unknown values are sent as null / "".
    """
    device_key: Annotated[str, Field(max_length=64), AfterValidator(_device_key)]
    browser: Text = ""
    browser_version: Text = ""
    os: Text = ""
    os_version: Text = ""
    cpu_cores: Optional[int] = Field(default=None, ge=0, le=4096)
    memory_gb: Optional[float] = Field(default=None, ge=0, le=4096)
    touch_points: Optional[int] = Field(default=None, ge=0, le=1024)
    gpu: Text = ""
    language: Text = ""
    timezone: Text = ""
    screen: Text = ""
    color_depth: Optional[int] = Field(default=None, ge=0, le=128)

    model_config = ConfigDict(extra="forbid")


# ---------- sign-in with the email code ----------

class AdminLoginVerification(BaseModel):
    """``POST /auth/login`` answer when the browser is not a trusted device: no tokens, a code was emailed."""
    status: Literal["verification_required"] = "verification_required"
    verification_id: str
    channel: Literal["email"] = "email"
    destination: str                 # masked email, e.g. "j***@m***.com"
    code_length: int                 # digits of the code (Supabase project setting, ADMIN_DEVICE_CODE_LENGTH)
    expires_in: int                  # seconds until the code expires (600)
    resend_available_in: int         # seconds until "Resend" is allowed (60)
    resends_left: int                # 3 at first


class AdminVerifyDeviceRequest(BaseModel):
    """Body of ``POST /auth/verify-device``."""
    verification_id: Annotated[str, Field(max_length=64)]
    code: Annotated[str, Field(min_length=1, max_length=32)]   # digits only, exact length checked by the service (422)
    trust_device: bool = False
    device_name: DeviceName = None
    device: AdminDeviceInfo

    model_config = ConfigDict(extra="forbid")


class AdminResendCodeRequest(BaseModel):
    """Body of ``POST /auth/resend-code``."""
    verification_id: Annotated[str, Field(max_length=64)]

    model_config = ConfigDict(extra="forbid")


class AdminCodeResent(BaseModel):
    expires_in: int
    resend_available_in: int
    resends_left: int


# ---------- devices lists ----------

class AdminDeviceRow(BaseModel):
    """One row of "My devices" and of an admin's devices (super admin)."""
    id: str
    name: Optional[str] = None       # the admin's own name for it, else null (the portal shows "Chrome on Windows")
    browser: str = ""
    browser_version: str = ""
    os: str = ""
    os_version: str = ""
    timezone: str = ""
    screen: str = ""
    language: str = ""
    first_ip: Optional[str] = None
    last_ip: Optional[str] = None
    created_at: datetime
    last_used_at: datetime
    trusted: bool                    # trusted_until in the future
    trusted_until: Optional[datetime] = None
    signed_in: bool                  # the row has a live session
    current: bool                    # the caller's own browser (always false in another admin's list)


class AdminDeviceList(BaseModel):
    data: List[AdminDeviceRow]
    total_count: int


class AdminDeviceRenameRequest(BaseModel):
    name: Annotated[str, Field(max_length=200), AfterValidator(_device_name)]

    model_config = ConfigDict(extra="forbid")


class AdminDevicesRevoked(BaseModel):
    revoked: int
