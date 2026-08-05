"""Dependencies used only by the MCP namespace.

Nothing here is imported by the legacy routers, so no legacy request can ever be
subjected to the feature flag or to the idempotency requirement.
"""

from fastapi import Header

from app.config.feature_flags import is_mcp_card_purchase_enabled, is_mcp_purchase_enabled
from app.config.mcp_card_constants import McpCardErrorMessages
from app.config.mcp_constants import McpErrorMessages
from app.exceptions import CustomException
from app.services.mcp_idempotency import require_hash_secret, validate_idempotency_key


def mcp_feature_enabled() -> bool:
    """Reject the request with a safe feature-disabled response while the flag is false."""
    if not is_mcp_purchase_enabled():
        raise CustomException(code=503, name=McpErrorMessages.MCP_PURCHASE_DISABLED,
                              details="MCP purchase is not enabled in this environment")
    return True


def mcp_card_feature_enabled() -> bool:
    """Reject MCP card requests with a safe 503 while ``MCP_CARD_PURCHASE_ENABLED`` is false.

    Independent of the wallet flag: neither flag affects the other endpoint, and no flag
    here can reach a legacy route.
    """
    if not is_mcp_card_purchase_enabled():
        raise CustomException(code=503, name=McpCardErrorMessages.MCP_CARD_PURCHASE_DISABLED,
                              details="MCP card purchase is not enabled in this environment")
    return True


def mcp_secret_configured() -> bool:
    """Fail closed when the feature is on but its hash secret is unusable.

    Ordered after ``mcp_feature_enabled`` so a deployment that never enables MCP is
    never asked for a secret. The secret's value is never logged or returned.
    """
    require_hash_secret()
    return True


def idempotency_key_header(
        idempotency_key: str = Header(None, alias="Idempotency-Key",
                                      description="Opaque idempotency key, 32-128 safe characters")) -> str:
    """Validate the Idempotency-Key header. The raw value is never logged."""
    return validate_idempotency_key(idempotency_key)
