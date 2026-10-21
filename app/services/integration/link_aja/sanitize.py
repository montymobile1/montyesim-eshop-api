"""Redaction helpers so LinkAja secrets never reach logs or clients.

Used by the HTTP client for every request/response it logs, and by the service
layer before logging any provider payload. Kept as a standalone module so the
rule "no secret ever hits a log line" is enforced in exactly one place and is
directly unit-testable.
"""

from typing import Any, Mapping

REDACTED = "***REDACTED***"

# Matched case-insensitively as substrings, so `X-Signature`, `client_secret`,
# `Authorization` and `access_token` are all covered without enumerating the
# provider's (still unknown) exact header and field names.
SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "secret",
    "token",
    "password",
    "signature",
    "credential",
    "client_id",
    "apikey",
    "api_key",
    "private",
)


def is_sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact(value: Any) -> Any:
    """Recursively replace values of sensitive-looking keys with a placeholder.

    Non-mapping, non-sequence values are returned unchanged - the caller is
    responsible for not passing a bare secret in as the top-level value.
    """
    if isinstance(value, Mapping):
        return {k: (REDACTED if is_sensitive_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def safe_url(url: str) -> str:
    """Strip any query string from a URL before logging it.

    Some providers place tokens in query parameters; dropping the query removes
    that risk entirely and the path alone is what is useful for debugging.
    """
    if not url:
        return ""
    return str(url).split("?", 1)[0]
