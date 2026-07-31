"""Request/response contracts owned by the MCP purchase endpoints.

These models are intentionally separate from ``app.schemas.bundle`` so that the
legacy ``AssignRequest``/``PaymentIntentResponse`` contract used by the mobile and
web clients can never be changed by MCP requirements.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.config.mcp_constants import (
    McpNextAction,
    McpOrderStatus,
    McpPaymentStatus,
    McpProvisioningStatus,
    McpPurchaseStatus,
)


class McpCountryRequest(BaseModel):
    iso3_code: str = Field(min_length=1, max_length=8)
    country_name: str = Field(min_length=1, max_length=120)

    model_config = ConfigDict(extra="forbid")


class McpRegionRequest(BaseModel):
    iso_code: str = Field(min_length=1, max_length=16)
    region_name: str = Field(min_length=1, max_length=120)

    model_config = ConfigDict(extra="forbid")


class McpRelatedSearchRequest(BaseModel):
    region: Optional[McpRegionRequest] = None
    countries: Optional[List[McpCountryRequest]] = Field(default=None, max_length=250)

    model_config = ConfigDict(extra="forbid")


class McpAssignRequest(BaseModel):
    """Body accepted by ``POST /api/v1/mcp/user/bundle/assign``.

    ``extra="forbid"`` guarantees that price, tax, wallet balance, user id, email,
    tokens, payment status or order status can never be supplied by the caller:
    any unknown field is rejected with a validation error.
    """

    bundle_code: str = Field(min_length=1, max_length=200)
    payment_type: Literal["Wallet"] = "Wallet"
    related_search: Optional[McpRelatedSearchRequest] = None
    quote_reference: Optional[str] = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$",
        description="Opaque MCP correlation reference. Never trusted for idempotency or pricing.")

    model_config = ConfigDict(extra="forbid")


class McpPurchaseResponse(BaseModel):
    """Dedicated MCP purchase result.

    Deliberately excludes tokens, the raw idempotency key, wallet transaction
    internals, provider credentials, activation code and ICCID.
    """

    status: McpPurchaseStatus
    order_id: Optional[str] = None
    payment_method: Literal["Wallet"] = "Wallet"
    payment_status: McpPaymentStatus
    order_status: McpOrderStatus
    idempotent_replay: bool = False
    provisioning_status: McpProvisioningStatus
    next_action: McpNextAction
    quote_reference: Optional[str] = None
    correlation_id: Optional[str] = None
    message: Optional[str] = None

    model_config = ConfigDict(extra="forbid")
