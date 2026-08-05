"""Constants owned exclusively by the MCP Stripe hosted-checkout feature (Phase 5A).

Kept apart from ``mcp_constants`` so the Wallet contract and the Card contract can
evolve independently, and apart from every legacy payment constant so nothing here
can reach the legacy Card flow.
"""

from enum import StrEnum

#: Operation identifier bound into the idempotency identity for MCP card checkouts.
#: It is deliberately different from MCP_WALLET_BUNDLE_ASSIGN: the identity of the
#: shared ``mcp_purchase_idempotency`` table is (user_id, operation, key_hash), so the
#: same external Idempotency-Key may safely be presented for a wallet purchase and a
#: card checkout without either colliding with the other.
MCP_CARD_BUNDLE_CHECKOUT = "MCP_CARD_BUNDLE_CHECKOUT"

#: Feature flag guarding every MCP card endpoint. Defaults to disabled.
MCP_CARD_PURCHASE_ENABLED_FLAG = "MCP_CARD_PURCHASE_ENABLED"

#: Environment variables holding the validated redirect targets.
CARD_SUCCESS_URL_ENV = "MCP_CARD_SUCCESS_URL"
CARD_CANCEL_URL_ENV = "MCP_CARD_CANCEL_URL"

#: Checkout Session lifetime. Stripe permits 30 minutes .. 24 hours.
CARD_SESSION_EXPIRY_MINUTES_ENV = "MCP_CARD_SESSION_EXPIRY_MINUTES"
DEFAULT_SESSION_EXPIRY_MINUTES = 30
MIN_SESSION_EXPIRY_MINUTES = 30
MAX_SESSION_EXPIRY_MINUTES = 24 * 60

#: Bounded length for the opaque references we accept or emit.
QUOTE_REFERENCE_MAX_LENGTH = 128
PAYMENT_REFERENCE_MAX_LENGTH = 64
BUNDLE_CODE_MAX_LENGTH = 200

#: Stripe fixes the payment surface for this feature; nothing is caller supplied.
STRIPE_PAYMENT_METHOD_TYPES = ["card"]
STRIPE_MODE = "payment"

#: Stripe events this feature consumes.
#:
#: These are the SAME events the legacy Card flow already relies on, so enabling MCP
#: card checkout requires no new webhook subscription in the Stripe dashboard. A hosted
#: Checkout Session in ``mode=payment`` creates a PaymentIntent up front and emits
#: ``payment_intent.succeeded`` on payment, exactly like the legacy native-SDK flow.
#:
#: ``payment_intent.failed`` is included because the legacy constant uses that (non
#: standard) name; ``payment_intent.payment_failed`` is Stripe's real event. Accepting
#: both costs nothing and means the feature works whichever is actually subscribed.
PAYMENT_INTENT_SUCCEEDED = "payment_intent.succeeded"
PAYMENT_INTENT_PAYMENT_FAILED = "payment_intent.payment_failed"
PAYMENT_INTENT_FAILED = "payment_intent.failed"
PAYMENT_INTENT_CANCELED = "payment_intent.canceled"

MCP_CARD_WEBHOOK_EVENTS = frozenset({
    PAYMENT_INTENT_SUCCEEDED,
    PAYMENT_INTENT_PAYMENT_FAILED,
    PAYMENT_INTENT_FAILED,
    PAYMENT_INTENT_CANCELED,
})

#: Metadata key stamped on every PaymentIntent (and Session) this feature creates.
#:
#: This marker is what keeps the two flows apart now that both ride the same event
#: type. Routing is by marker, never by event type alone:
#:   * marker present  -> MCP card handler (dedupe + compare-and-set + verification)
#:   * marker absent   -> the pre-existing legacy handler, unchanged
#: A legacy PaymentIntent never carries it, so the legacy path is untouched.
METADATA_SOURCE_KEY = "mcp_source"
METADATA_SOURCE_VALUE = "mcp_card_checkout_v1"


class McpCardStatus(StrEnum):
    """Normalized, caller-safe payment states.

    These never expose Stripe's vocabulary directly: a client integrating against
    this endpoint must not have to reason about Stripe session/intent statuses.
    """

    PENDING = "PENDING"           # session created, awaiting payment
    PAID = "PAID"                 # payment verified by webhook, not yet provisioned
    PROVISIONING = "PROVISIONING" # provisioning claimed by exactly one worker
    COMPLETED = "COMPLETED"       # eSIM provisioned
    FAILED = "FAILED"             # payment failed
    EXPIRED = "EXPIRED"           # checkout session expired unpaid
    CANCELLED = "CANCELLED"       # explicitly cancelled
    AMBIGUOUS = "AMBIGUOUS"       # paid but provisioning unresolved -> manual intervention


#: States from which provisioning may still be claimed.
PROVISIONABLE_STATES = frozenset({McpCardStatus.PAID})

#: Terminal states: a webhook must never move the record out of these.
TERMINAL_STATES = frozenset({
    McpCardStatus.COMPLETED,
    McpCardStatus.FAILED,
    McpCardStatus.EXPIRED,
    McpCardStatus.CANCELLED,
})


class McpCardErrorMessages(StrEnum):
    """Error identifiers used only by MCP card endpoints (translated via locales)."""

    MCP_CARD_PURCHASE_DISABLED = "MCP_CARD_PURCHASE_DISABLED"
    MCP_CARD_CONFIG_INVALID = "MCP_CARD_CONFIG_INVALID"
    MCP_CARD_QUOTE_INVALID = "MCP_CARD_QUOTE_INVALID"
    MCP_CARD_PAYMENT_NOT_FOUND = "MCP_CARD_PAYMENT_NOT_FOUND"
    MCP_CARD_CHECKOUT_UNAVAILABLE = "MCP_CARD_CHECKOUT_UNAVAILABLE"
    MCP_CARD_SESSION_EXPIRED = "MCP_CARD_SESSION_EXPIRED"
