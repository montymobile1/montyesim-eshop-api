"""Environment driven feature flags.

Booleans are parsed with the same convention already used across the project
(``os.getenv("X", "false").lower() in ("true", "1", "yes")``) so operators do not
have to learn a second syntax.

Flags are read on every call (never cached at import time) so that a deployment
can flip a flag with a restart only, and so tests can toggle them safely.
"""

import os

_TRUTHY = ("true", "1", "yes", "on")

#: Name of the flag guarding the MCP purchase endpoints. Defaults to disabled.
MCP_PURCHASE_ENABLED_FLAG = "MCP_PURCHASE_ENABLED"


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    """Parse a boolean flag value safely. Unknown/blank values fall back to ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized == "":
        return default
    return normalized in _TRUTHY


def get_bool_env(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable, defaulting to ``default`` when unset/blank."""
    return parse_bool(os.getenv(name), default=default)


def is_mcp_purchase_enabled() -> bool:
    """True only when ``MCP_PURCHASE_ENABLED`` is explicitly truthy. Defaults to False."""
    return get_bool_env(MCP_PURCHASE_ENABLED_FLAG, default=False)
