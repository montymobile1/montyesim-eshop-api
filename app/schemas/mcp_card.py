"""Request and response shapes for the MCP card checkout endpoints.

The request model is the load-bearing one. ``extra="forbid"`` with exactly three declared
fields means an MCP client structurally cannot send a card number, an expiry, a security
code, a cardholder name, a billing address, a payment token, an amount, a currency, a
``paid`` flag, a ``checkout_url`` or a ``payment_type``: any of them is a ``422`` for the
whole request. The card is entered on the provider's own hosted page, which this backend
never renders and never proxies.

The response models carry only values that are safe to hand to a chat client: the link,
an opaque reference that is the existing ``user_order.id``, the amount, the currency, an
expiry and a status word. No Stripe session id, client secret, publishable key, customer
id or ephemeral key appears in any of them.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.config.mcp_constants import McpCardStatus, McpNextAction
from app.schemas.mcp import MAX_BUNDLE_CODE_LENGTH, MAX_QUOTE_REFERENCE_LENGTH, McpRelatedSearchRequest


class McpCardCheckoutRequest(BaseModel):
    """Body of ``POST /api/v1/mcp/user/bundle/card/checkout``.

    Three fields, and no more. There is deliberately no ``payment_type``: this route *is*
    the card route, so the backend fixes the payment type internally and a client sending
    one is refused rather than obeyed.
    """

    model_config = ConfigDict(extra="forbid")

    bundle_code: str = Field(min_length=1, max_length=MAX_BUNDLE_CODE_LENGTH)
    quote_reference: Optional[str] = Field(default=None, max_length=MAX_QUOTE_REFERENCE_LENGTH)
    related_search: Optional[McpRelatedSearchRequest] = None


class McpCardCheckoutResponse(BaseModel):
    """The hosted payment page that was opened. Nothing has been charged.

    ``status`` is fixed at ``PENDING`` at creation time and there is no code path that can
    set anything else here: only the existing signature-verified Stripe webhook can move a
    payment forward, and it does so by writing the order row this response points at.
    """

    model_config = ConfigDict(extra="forbid")

    payment_reference: str
    order_id: str
    checkout_url: Optional[str] = None
    status: McpCardStatus = McpCardStatus.PENDING
    #: Decimal string in the settlement currency, e.g. ``"10.00"``. A string rather than a
    #: float so no binary-rounding artefact can be shown to a user as a price.
    amount: Optional[str] = None
    currency: Optional[str] = None
    #: ISO-8601 UTC instant after which the hosted page stops accepting a payment.
    expires_at: Optional[str] = None
    next_action: Optional[McpNextAction] = McpNextAction.OPEN_CHECKOUT_URL
    #: Always ``False`` -- see :class:`~app.schemas.mcp.McpAssignResponse`.
    idempotent_replay: bool = False
    message: Optional[str] = None


class McpCardStatusResponse(BaseModel):
    """What happened to one card payment, read from existing order records only.

    ``paid`` and ``provisioned`` are kept separate on purpose. Money arriving and an eSIM
    existing are different facts, and a payment whose eSIM is not ready must never be
    described as ready.
    """

    model_config = ConfigDict(extra="forbid")

    payment_reference: str
    status: McpCardStatus
    order_id: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    bundle_code: Optional[str] = None
    #: Not persisted by this branch (that would need a new column), so always ``None``.
    quote_reference: Optional[str] = None
    expires_at: Optional[str] = None
    paid: bool = False
    provisioned: bool = False
    next_action: Optional[McpNextAction] = None
    message: Optional[str] = None
