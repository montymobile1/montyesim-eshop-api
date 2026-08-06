"""Constants owned exclusively by the ``/api/v1/mcp/...`` adapter.

Kept out of :mod:`app.config.constants` on purpose. ``ErrorMessages`` is read by the
legacy request path, the webhook and the mobile/web clients; adding MCP members to it
would put MCP vocabulary on a surface that legacy traffic depends on. Nothing in this
module is imported by a legacy module, so removing the MCP routes would remove it
entirely without touching anything else.
"""

from enum import StrEnum


class McpErrorMessages(StrEnum):
    """Stable, safe error codes returned only by the MCP endpoints.

    These are the *keys* the exception handler translates through ``locales/``; they
    never carry a token, a Stripe secret, an OTP, a personal identifier or an internal
    exception message. Errors that already have a legacy meaning (an invalid bundle, an
    insufficient wallet balance, a missing bearer token) deliberately reuse the existing
    ``ErrorMessages`` member instead of being duplicated here, so the MCP client sees the
    same code the rest of the platform emits for the same condition.
    """

    #: The MCP purchase surface is switched off for this deployment.
    MCP_PURCHASE_DISABLED = "MCP_PURCHASE_DISABLED"
    #: The MCP card checkout surface specifically is switched off.
    MCP_CARD_PURCHASE_DISABLED = "MCP_CARD_PURCHASE_DISABLED"
    #: An anonymous/guest session reached an MCP endpoint. Never allowed.
    MCP_ANONYMOUS_NOT_ALLOWED = "MCP_ANONYMOUS_NOT_ALLOWED"
    #: ``X-Currency`` names a currency these endpoints cannot settle in.
    MCP_UNSUPPORTED_CURRENCY = "MCP_UNSUPPORTED_CURRENCY"
    #: A deployment enabled card checkout without valid redirect configuration.
    MCP_CARD_CONFIG_INVALID = "MCP_CARD_CONFIG_INVALID"
    #: The payment provider could not open a hosted page. Nothing was charged.
    MCP_CARD_CHECKOUT_UNAVAILABLE = "MCP_CARD_CHECKOUT_UNAVAILABLE"
    #: No payment with that reference belongs to the authenticated caller.
    MCP_PAYMENT_NOT_FOUND = "MCP_PAYMENT_NOT_FOUND"
    #: The purchase completed a step that cannot be undone and needs a human.
    MCP_MANUAL_INTERVENTION_REQUIRED = "MCP_MANUAL_INTERVENTION_REQUIRED"
    #: A backend dependency (payment provider, eSIM hub, database) is unavailable.
    MCP_DEPENDENCY_UNAVAILABLE = "MCP_DEPENDENCY_UNAVAILABLE"
    #: The request outlived its budget. The outcome is unknown, never "failed".
    MCP_TIMEOUT = "MCP_TIMEOUT"


class McpPurchaseStatus(StrEnum):
    """Business outcome of one MCP wallet purchase, as reported to the MCP client."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PENDING = "PENDING"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


class McpCardStatus(StrEnum):
    """Lifecycle of one MCP card payment, derived from existing order columns only.

    The words are the ones the external MCP service already normalizes against. They are
    deliberately *not* Stripe's vocabulary: nothing here is read from a redirect, and no
    provider spelling reaches the MCP client.
    """

    PENDING = "PENDING"
    PAID = "PAID"
    PROVISIONING = "PROVISIONING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    #: The platform itself cannot say what happened. Stop and escalate; never guess.
    AMBIGUOUS = "AMBIGUOUS"


class McpNextAction(StrEnum):
    """What the MCP client should do next. Never an instruction to pay again."""

    GET_ESIM_BY_ORDER = "GET_ESIM_BY_ORDER"
    OPEN_CHECKOUT_URL = "OPEN_CHECKOUT_URL"
    CONTACT_SUPPORT = "CONTACT_SUPPORT"
    NEW_CHECKOUT_REQUIRED = "NEW_CHECKOUT_REQUIRED"


#: Environment variable names. Read at call time rather than import time so a running
#: deployment can be flipped without a redeploy, and so tests can toggle them.
MCP_PURCHASE_ENABLED_ENV = "MCP_PURCHASE_ENABLED"
MCP_CARD_PURCHASE_ENABLED_ENV = "MCP_CARD_PURCHASE_ENABLED"
MCP_CARD_SUCCESS_URL_ENV = "MCP_CARD_SUCCESS_URL"
MCP_CARD_CANCEL_URL_ENV = "MCP_CARD_CANCEL_URL"
MCP_CARD_SESSION_EXPIRY_MINUTES_ENV = "MCP_CARD_SESSION_EXPIRY_MINUTES"

#: Stripe accepts a Checkout Session expiry between 30 minutes and 24 hours *after the
#: Session is created*. The timestamp is computed a moment before the call, so the bounds
#: are pulled one minute inside Stripe's window: an exact 30 would land just under it by
#: the time the request arrives and be rejected.
MIN_CARD_SESSION_EXPIRY_MINUTES = 31
MAX_CARD_SESSION_EXPIRY_MINUTES = 24 * 60 - 1
DEFAULT_CARD_SESSION_EXPIRY_MINUTES = 31

#: Descriptive marker written into Stripe metadata for support triage. The existing
#: webhook never reads it: it reads the keys it has always read and ignores the rest.
MCP_SOURCE_MARKER = "mcp_hosted_checkout_v1"
MCP_SOURCE_METADATA_KEY = "mcp_source"

#: Prefix of the Stripe idempotency key used for a hosted checkout. Suffixed with the
#: database-generated ``user_order.id``, which is already persisted, so no MCP-specific
#: storage is required to make the Stripe call itself repeatable.
MCP_CARD_STRIPE_IDEMPOTENCY_PREFIX = "mcp-card-"
