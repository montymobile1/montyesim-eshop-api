"""Request/response contracts for the MCP Stripe hosted-checkout endpoints.

Separate from ``app.schemas.mcp`` (Wallet) and from ``app.schemas.bundle`` (legacy) so
neither contract can be changed by card requirements.

The request model accepts *only* quote identity. ``extra="forbid"`` means a caller
cannot supply - and the server can therefore never be tricked into trusting - a card
number, CVC, expiry, PaymentMethod id, Stripe token, amount, tax or final price.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.config.mcp_card_constants import (
    BUNDLE_CODE_MAX_LENGTH,
    QUOTE_REFERENCE_MAX_LENGTH,
    McpCardStatus,
)
from app.schemas.mcp import McpRelatedSearchRequest


class McpCardCheckoutRequest(BaseModel):
    """Body accepted by ``POST /api/v1/mcp/user/bundle/card/checkout``.

    ``payment_type`` is absent on purpose: it is fixed internally to Card/Stripe and
    cannot be influenced by the caller.
    """

    bundle_code: str = Field(min_length=1, max_length=BUNDLE_CODE_MAX_LENGTH)
    quote_reference: str = Field(
        min_length=1, max_length=QUOTE_REFERENCE_MAX_LENGTH, pattern=r"^[A-Za-z0-9._:\-]+$",
        description="Opaque MCP quote reference. Part of the idempotency identity; "
                    "never trusted for pricing.")
    related_search: Optional[McpRelatedSearchRequest] = None

    model_config = ConfigDict(extra="forbid")


class McpCardCheckoutResponse(BaseModel):
    """Safe checkout result. Carries no Stripe secret of any kind.

    Deliberately excluded: Session client_secret, PaymentIntent client_secret, the
    Stripe session id, customer id, ephemeral keys, publishable/secret keys, raw Stripe
    payloads, and the caller's idempotency key.
    """

    payment_reference: str
    order_id: Optional[str] = None
    checkout_url: Optional[str] = None
    status: McpCardStatus
    amount: str = Field(description="Decimal string in major units, e.g. '10.00'.")
    currency: str
    expires_at: Optional[str] = None
    idempotent_replay: bool = False
    correlation_id: Optional[str] = None
    message: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class McpCardStatusResponse(BaseModel):
    """Safe polling result for ``GET .../card/status/{payment_reference}``."""

    payment_reference: str
    order_id: Optional[str] = None
    status: McpCardStatus
    amount: str
    currency: str
    bundle_code: str
    quote_reference: Optional[str] = None
    expires_at: Optional[str] = None
    provisioned: bool = False
    next_action: str
    message: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class McpCardNextAction:
    """Caller guidance strings. Not an enum on the wire to keep the contract additive."""

    OPEN_CHECKOUT_URL = "OPEN_CHECKOUT_URL"
    WAIT_FOR_CONFIRMATION = "WAIT_FOR_CONFIRMATION"
    GET_ESIM_BY_ORDER = "GET_ESIM_BY_ORDER"
    CONTACT_SUPPORT = "CONTACT_SUPPORT"
    START_NEW_CHECKOUT = "START_NEW_CHECKOUT"


#: Mapping from a normalized status to the guidance returned with it.
STATUS_NEXT_ACTION = {
    McpCardStatus.PENDING: McpCardNextAction.OPEN_CHECKOUT_URL,
    McpCardStatus.PAID: McpCardNextAction.WAIT_FOR_CONFIRMATION,
    McpCardStatus.PROVISIONING: McpCardNextAction.WAIT_FOR_CONFIRMATION,
    McpCardStatus.COMPLETED: McpCardNextAction.GET_ESIM_BY_ORDER,
    McpCardStatus.FAILED: McpCardNextAction.START_NEW_CHECKOUT,
    McpCardStatus.EXPIRED: McpCardNextAction.START_NEW_CHECKOUT,
    McpCardStatus.CANCELLED: McpCardNextAction.START_NEW_CHECKOUT,
    McpCardStatus.AMBIGUOUS: McpCardNextAction.CONTACT_SUPPORT,
}
