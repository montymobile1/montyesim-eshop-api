"""How the API recognises an admin's browser (TEOS-255).

``device_hash`` = HMAC-SHA256(server secret, JSON array of the random device key the portal keeps in the browser +
the browser and OS families). The JSON array (fixed order) avoids separator collisions; the secret means a
database leak does not let anyone rebuild a hash. The IP is NOT part of it (it changes between networks and is
shared by a whole office): it is stored on the row for display only.

Secret: ``ADMIN_DEVICE_HASH_SECRET`` when set, else derived from the service key ``SUPABASE_KEY`` (always set),
so no new setting is required. Changing the secret makes every browser "new" once (a code is asked).
"""
import hashlib
import hmac
import json
import os
from typing import Any, Dict

from app.schemas.admin.trusted_devices import AdminDeviceInfo

#: the values that go into the hash, in this order (never reorder: every stored hash would change)
#: Only the values that never change on their own: the random device key (what makes the browser unique) and
#: the browser and OS families. GPU, language, memory etc. can change mid-session (GPU switch, WebGL failure,
#: language setting) and would sign the admin out; they add no protection once the device key is there.
HASHED_FIELDS = ("device_key", "browser", "os")
#: stored in ``device_info`` for the Devices page and the emails
DISPLAY_FIELDS = ("browser", "browser_version", "os", "os_version", "timezone", "screen", "color_depth", "language")


def _secret() -> bytes:
    explicit = (os.getenv("ADMIN_DEVICE_HASH_SECRET") or "").strip()
    if explicit:
        return explicit.encode("utf-8")
    service_key = os.getenv("SUPABASE_KEY") or ""
    return hashlib.sha256(b"admin-device-hash:" + service_key.encode("utf-8")).digest()


def _normalised(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def device_hash(device: AdminDeviceInfo) -> str:
    values = [_normalised(getattr(device, name)) for name in HASHED_FIELDS]
    message = json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def display_info(device: AdminDeviceInfo) -> Dict[str, Any]:
    """The values shown on the Devices page and in the emails (never the device key)."""
    return {name: getattr(device, name) for name in DISPLAY_FIELDS}


def device_label(info: Dict[str, Any]) -> str:
    """"Chrome on Windows" from a stored ``device_info``."""
    browser = (info or {}).get("browser") or "Unknown browser"
    os_name = (info or {}).get("os") or "unknown OS"
    return f"{browser} on {os_name}"
