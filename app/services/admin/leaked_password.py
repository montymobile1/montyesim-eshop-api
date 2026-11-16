"""TEOS-334: refuse leaked passwords for the admin password change and reset, whatever the Supabase plan or setting.

Have I Been Pwned's "Pwned Passwords" range API (the list Supabase's own "leaked password protection" uses), with
k-anonymity: only the first 5 characters of the password's SHA-1 leave the server. The answer lists every leaked hash
starting with them (the rest of the hash and a count); the password is leaked when the rest of OUR hash is there.
``Add-Padding`` hides the real answer size (padded lines have count 0 and are ignored). Nothing is stored.

Fail open: when HIBP doesn't answer (timeout, network, 5xx, unreadable answer) the check logs a warning and lets the
password through; our own rules (``check_password_rules``) still apply. ``ADMIN_PASSWORD_LEAK_CHECK`` = off / false /
0 / no disables the check (e.g. where Supabase's own check is on or the server has no internet access).
"""
import hashlib
import os
from typing import Callable, Optional

import httpx
from loguru import logger

RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"
TIMEOUT_SECONDS = 3.0
#: generic client name: no project or user details go to the outside service
USER_AGENT = "admin-api-password-check"


def leak_check_enabled() -> bool:
    value = os.getenv("ADMIN_PASSWORD_LEAK_CHECK")
    if value is None or not value.strip():
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def _fetch_range(prefix: str) -> str:
    """The HIBP answer for a 5-character SHA-1 prefix (raises on any failure)."""
    response = httpx.get(RANGE_URL.format(prefix=prefix), timeout=TIMEOUT_SECONDS,
                         headers={"Add-Padding": "true", "User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.text


#: module level so tests can replace it (no network in tests)
range_fetcher: Callable[[str], str] = _fetch_range


def is_leaked(password: str) -> bool:
    """True when ``password`` is in HIBP's list of leaked passwords. False when it is not, when the check is
    disabled, or when HIBP could not be asked (fail open)."""
    if not password or not leak_check_enabled():
        return False
    # SHA-1 only because HIBP's range API is keyed by SHA-1 prefixes: the hash is never stored nor used to protect
    # anything, and only its first 5 characters leave the server (k-anonymity)
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        body = range_fetcher(prefix)
    except Exception as exc:
        logger.warning(f"leaked password check unavailable, password accepted: {type(exc).__name__}")
        return False
    return _count(body, suffix) > 0


def _count(body: Optional[str], suffix: str) -> int:
    """How often ``suffix`` was leaked according to the range answer (0 when absent or padded)."""
    for line in (body or "").splitlines():
        candidate, _, count = line.strip().partition(":")
        if candidate.upper() == suffix:
            try:
                return int(count.strip() or 0)
            except ValueError:
                return 0
    return 0
