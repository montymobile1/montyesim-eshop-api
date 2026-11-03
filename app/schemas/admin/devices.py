"""Shapes of GET /api/v1/admin/devices (MS-5081, TEOS-115).

The list takes ``user_id`` (a UUID, else 422), ``platform``, ``logged_in``, ``sort``, ``dir`` and
paging; no request body. Every filter is a closed list: any other value is a 422, so the client can
never choose the column the database sorts on. Rows are ``device`` rows without ``fcm_token`` and
``originated_ip`` (see ``app/repo/admin/device_repo.py``), plus ``user_email`` joined server-side.
"""
from enum import Enum


class DevicePlatform(str, Enum):
    """``device.os`` matched case-insensitively (the app sends the OS name in that column)."""
    all = "all"
    android = "android"
    ios = "ios"


class DeviceLoggedIn(str, Enum):
    """``device.is_logged_in``: yes = true, no = false."""
    all = "all"
    yes = "yes"
    no = "no"


class DeviceSort(str, Enum):
    created_at = "created_at"
    timestamp_login = "timestamp_login"
