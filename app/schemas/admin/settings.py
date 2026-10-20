"""Shapes of /api/v1/admin/settings and /settings/history (MS-5091, MS-5046).

- ``GET /settings`` rows: ``{id, key, value, created_at, is_secret}``. A legacy secret-shaped row
  still present in app_config is listed with ``value = "***"`` and ``is_secret = true``.
- ``PUT /settings`` body: :class:`AdminSettingsUpdate`; response :class:`AdminSettingsUpdateResult`.
- ``GET /settings/history`` rows: :class:`AdminSettingsHistoryRow`.
"""
import re
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.helper import is_secret_key

CACHE_KEY = "APP_CACHE_KEY"
#: Key format for a NEW setting. Existing keys (some are lower case, e.g. ``login_type``) can still
#: be edited and deleted; the service applies this pattern to keys that do not exist yet.
NEW_KEY_PATTERN = re.compile(r"^[A-Z0-9_]{1,100}$")
MAX_VALUE_LENGTH = 10_000
MAX_ITEMS = 500


class AdminSettingItem(BaseModel):
    """One key to create or update. ``key``: 1-100 characters, no spaces; ``value``: up to 10 000."""
    key: str = Field(..., min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.\-]+$")
    value: str = Field(..., max_length=MAX_VALUE_LENGTH)

    model_config = ConfigDict(extra="forbid")


class AdminSettingsUpdate(BaseModel):
    """Body of ``PUT /settings``: the keys to create or update and the keys to delete, in one save.

    422 when both lists are empty, when a key appears twice, when a key is in both lists, when
    ``APP_CACHE_KEY`` is in either list (the server rotates it), or when an item key is secret-shaped
    (secrets live in the environment, never in app_config). Error texts start with ``items.<index>.key``
    or ``deleted_keys.<index>`` so the portal can mark the row.
    """
    items: List[AdminSettingItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    deleted_keys: List[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    reason: Optional[str] = Field(None, min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check(self) -> "AdminSettingsUpdate":
        if not self.items and not self.deleted_keys:
            raise ValueError("items and deleted_keys are both empty: nothing to save")
        seen = set()
        for index, item in enumerate(self.items):
            if item.key == CACHE_KEY:
                raise ValueError(f"items.{index}.key: cache_key_forbidden, APP_CACHE_KEY is rotated by the server")
            if is_secret_key(item.key):
                raise ValueError(f"items.{index}.key: secret_key, secrets are set in the environment, "
                                 f"not in settings")
            if item.key in seen:
                raise ValueError(f"items.{index}.key: duplicate_key {item.key}")
            seen.add(item.key)
        deleted_seen = set()
        for index, key in enumerate(self.deleted_keys):
            if not key or len(key) > 100:
                raise ValueError(f"deleted_keys.{index}: invalid key")
            if key == CACHE_KEY:
                raise ValueError(f"deleted_keys.{index}: cache_key_forbidden, APP_CACHE_KEY cannot be deleted")
            if key in seen:
                raise ValueError(f"deleted_keys.{index}: {key} is also in items")
            if key in deleted_seen:
                raise ValueError(f"deleted_keys.{index}: duplicate_key {key}")
            deleted_seen.add(key)
        return self


class AdminSettingsUpdateResult(BaseModel):
    updated: List[str]
    deleted: List[str]
    cache_key_rotated: bool


class AdminSettingRow(BaseModel):
    id: Any
    key: str
    value: Optional[str] = None
    created_at: Optional[datetime] = None
    is_secret: bool = False


class AdminActor(BaseModel):
    id: str
    email: Optional[str] = None


class AdminSettingChange(BaseModel):
    """One changed key. ``old_value`` null = the key was created; ``new_value`` null = it was deleted.
    Secret-shaped keys have both values replaced by ``"***"`` and ``redacted = true``."""
    key: str
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    redacted: bool = False


class AdminSettingsHistoryRow(BaseModel):
    """One settings save. ``changes`` is computed on the server from old_data/new_data and lists only the
    keys whose value differs; values of keys matching SECRET|PASSWORD|TOKEN|PRIVATE|API_KEY (or the
    secret rule of app/config/helper.py, APP_CACHE_KEY excepted) are ``"***"``. ``parse_error`` is true
    when a legacy row's data could not be read (``changes`` is then empty)."""
    id: int
    changed_at: Optional[datetime] = None
    changed_by: Optional[AdminActor] = None
    ip_address: Optional[str] = None
    client_reported: bool = False
    reason: Optional[str] = None
    operation: Optional[str] = None
    changes: List[AdminSettingChange]
    parse_error: bool = False
