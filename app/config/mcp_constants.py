"""Constants owned exclusively by the MCP purchase feature.

Kept in a dedicated module so that nothing in the legacy purchase flow has to
change when the MCP contract evolves.
"""

from enum import StrEnum

#: Operation identifier bound into every idempotency identity for MCP wallet purchases.
MCP_WALLET_BUNDLE_ASSIGN = "MCP_WALLET_BUNDLE_ASSIGN"

#: Allowed characters for a caller supplied Idempotency-Key (opaque, URL-safe).
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._~\-]+$"
IDEMPOTENCY_KEY_MIN_LENGTH = 32
IDEMPOTENCY_KEY_MAX_LENGTH = 128

#: How long a terminal idempotency record can be replayed before the key may be recycled.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

#: How long a PROCESSING record is considered "a request that is still in flight"
#: before it is treated as a crashed execution and reconciled.
DEFAULT_PROCESSING_TIMEOUT_SECONDS = 120


class McpIdempotencyStatus(StrEnum):
    """Persisted lifecycle of one idempotency record."""

    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    AMBIGUOUS = "AMBIGUOUS"


class McpClaimOutcome(StrEnum):
    """Outcome returned by the atomic claim RPC."""

    CLAIMED = "CLAIMED"
    CONFLICT = "CONFLICT"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    AMBIGUOUS = "AMBIGUOUS"
    RETRY = "RETRY"


class McpPurchaseStatus(StrEnum):
    """Business outcome reported to the MCP caller."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


class McpProvisioningStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_STARTED = "NOT_STARTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class McpPaymentStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_CHARGED = "NOT_CHARGED"
    UNKNOWN_OR_SUCCESS = "UNKNOWN_OR_SUCCESS"


class McpOrderStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    NOT_CREATED = "NOT_CREATED"
    FAILURE_OR_UNKNOWN = "FAILURE_OR_UNKNOWN"


class McpNextAction(StrEnum):
    GET_ESIM_BY_ORDER = "GET_ESIM_BY_ORDER"
    CONTACT_SUPPORT = "CONTACT_SUPPORT"
    RETRY_WITH_NEW_IDEMPOTENCY_KEY = "RETRY_WITH_NEW_IDEMPOTENCY_KEY"
    RETRY_SAME_IDEMPOTENCY_KEY = "RETRY_SAME_IDEMPOTENCY_KEY"


class McpErrorMessages(StrEnum):
    """Error identifiers used only by MCP endpoints (translated via locales)."""

    MCP_PURCHASE_DISABLED = "MCP_PURCHASE_DISABLED"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_INVALID = "IDEMPOTENCY_KEY_INVALID"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    IDEMPOTENT_REQUEST_IN_PROGRESS = "IDEMPOTENT_REQUEST_IN_PROGRESS"
    MCP_UNSUPPORTED_PAYMENT_TYPE = "MCP_UNSUPPORTED_PAYMENT_TYPE"
    MCP_ANONYMOUS_NOT_ALLOWED = "MCP_ANONYMOUS_NOT_ALLOWED"
    MCP_MANUAL_INTERVENTION_REQUIRED = "MCP_MANUAL_INTERVENTION_REQUIRED"
    MCP_PURCHASE_TEMPORARILY_UNAVAILABLE = "MCP_PURCHASE_TEMPORARILY_UNAVAILABLE"


#: HTTP status returned when the wallet may have been debited but provisioning did not
#: complete. Deliberately distinct from 409 (idempotency semantics) so an MCP client can
#: tell "do not retry, escalate" apart from "retry the same key later".
HTTP_MANUAL_INTERVENTION = 424
