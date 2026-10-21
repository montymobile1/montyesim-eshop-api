"""Request and response shapes for the MCP purchase endpoints.

Every request model declares ``extra="forbid"``. That is the structural half of "never
trust MCP": an amount, a price, a currency, a balance, a ``user_id``, an ``order_id``, a
``checkout_url``, a ``paid`` flag or a card number sent by an MCP client is a ``422`` for
the whole request rather than a field the endpoint politely ignores. There is no field
here through which any of them could arrive, and no code path that adds one.

Every response model is a closed, safe field set. No token, refresh token, Stripe secret,
webhook secret, publishable key, client secret, ephemeral key, OTP, ICCID, activation
code or internal exception message appears in any of them.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.config.mcp_constants import McpNextAction, McpPurchaseStatus

#: Longest opaque correlation reference accepted from an MCP client. The value is echoed
#: back and never used to look anything up, price anything or authorize anything.
MAX_QUOTE_REFERENCE_LENGTH = 128

#: Longest bundle code accepted before the request is refused outright.
MAX_BUNDLE_CODE_LENGTH = 128


class McpRegionRequest(BaseModel):
    """Region half of the MCP related-search payload, mirroring ``RegionRequestDto``."""

    model_config = ConfigDict(extra="forbid")

    iso_code: str = Field(min_length=1, max_length=32)
    region_name: str = Field(min_length=1, max_length=128)


class McpCountryRequest(BaseModel):
    """Country half of the MCP related-search payload, mirroring ``CountryRequestDto``."""

    model_config = ConfigDict(extra="forbid")

    iso3_code: str = Field(min_length=1, max_length=32)
    country_name: str = Field(min_length=1, max_length=128)


class McpRelatedSearchRequest(BaseModel):
    """Descriptive search context, stored on the order exactly as the legacy flow does.

    Purely descriptive: it never influences the price, the bundle or the payment.
    """

    model_config = ConfigDict(extra="forbid")

    region: Optional[McpRegionRequest] = None
    countries: Optional[List[McpCountryRequest]] = Field(default=None, max_length=64)


class McpAssignRequest(BaseModel):
    """Body of ``POST /api/v1/mcp/user/bundle/assign``.

    ``payment_type`` is a ``Literal["Wallet"]`` rather than the full ``PaymentTypeEnum``:
    this endpoint is the wallet adapter, and a client asking it for a card or DCB purchase
    is refused at validation time instead of being silently re-routed.
    """

    model_config = ConfigDict(extra="forbid")

    bundle_code: str = Field(min_length=1, max_length=MAX_BUNDLE_CODE_LENGTH)
    payment_type: Literal["Wallet"] = "Wallet"
    quote_reference: Optional[str] = Field(default=None, max_length=MAX_QUOTE_REFERENCE_LENGTH)
    related_search: Optional[McpRelatedSearchRequest] = None


class McpAssignResponse(BaseModel):
    """Result of one MCP wallet purchase, built from what the shared flow actually did.

    ``status`` is never optimistic. It reports ``COMPLETED`` only when the order row the
    shared flow wrote says the payment succeeded *and* the order succeeded *and* a user
    profile exists for it -- that is, only when the existing flow confirms success.
    """

    model_config = ConfigDict(extra="forbid")

    status: McpPurchaseStatus
    order_id: Optional[str] = None
    payment_method: str = "Wallet"
    payment_status: Optional[str] = None
    order_status: Optional[str] = None
    provisioning_status: Optional[str] = None
    next_action: Optional[McpNextAction] = None
    quote_reference: Optional[str] = None
    #: Always ``False``. This branch adds no MCP persistence, so no durable replay of a
    #: previous answer is possible, and claiming one would be a guarantee that does not
    #: hold. See ``docs/mcp-endpoints.md``.
    idempotent_replay: bool = False
    message: Optional[str] = None
