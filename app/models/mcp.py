from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict


class McpPurchaseIdempotencyModel(BaseModel):
    """One durable idempotency record for an MCP purchase.

    ``idempotency_key_hash`` holds a keyed digest only - the raw Idempotency-Key
    is never modelled, stored or logged.
    """

    id: Optional[str] = None
    user_id: str
    operation: str
    idempotency_key_hash: str
    request_hash: str
    status: str
    order_id: Optional[str] = None
    response_code: Optional[int] = None
    response_body: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    #: True once an order row, wallet debit or provisioning call happened for this key.
    has_side_effects: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    #: Retention hint only; it never unlocks or recycles a terminal key.
    expires_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class McpIdempotencyClaim(BaseModel):
    """Result of the atomic claim RPC."""

    outcome: str
    record_id: Optional[str] = None
    status: Optional[str] = None
    order_id: Optional[str] = None
    response_code: Optional[int] = None
    response_body: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    request_hash: Optional[str] = None
    has_side_effects: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")
