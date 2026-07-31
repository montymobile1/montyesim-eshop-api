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

#: Retention hint written to ``expires_at``. It is metadata for an *offline* archival
#: policy only: a terminal record is NEVER recycled or re-executed when it elapses.
#: See ``migrations/20260731_0001_mcp_purchase_idempotency.sql``.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

#: Environment variable holding the HMAC secret used to digest Idempotency-Keys.
IDEMPOTENCY_HASH_SECRET_ENV = "MCP_IDEMPOTENCY_HASH_SECRET"

#: Minimum length of that secret. Short secrets are brute-forceable, and the digest
#: is the only thing standing between two callers' idempotency identities.
IDEMPOTENCY_HASH_SECRET_MIN_LENGTH = 32

#: Values that look like an unfilled template rather than a real secret. Compared
#: case-insensitively against the whole value and as a prefix, so "changeme-123"
#: is rejected too. The secret itself is never logged or echoed.
IDEMPOTENCY_HASH_SECRET_PLACEHOLDERS = (
    "change", "changeme", "change_me", "change-me", "placeholder", "secret", "mysecret",
    "your-secret", "your_secret", "yoursecret", "todo", "tbd", "fixme", "example",
    "test", "testing", "dummy", "sample", "replace", "replaceme", "none", "null",
    "password", "hash-secret", "hash_secret", "xxx", "abc", "123",
)

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
    MCP_IDEMPOTENCY_SECRET_MISCONFIGURED = "MCP_IDEMPOTENCY_SECRET_MISCONFIGURED"
    MCP_UNSUPPORTED_CURRENCY = "MCP_UNSUPPORTED_CURRENCY"


#: HTTP status returned when the wallet may have been debited but provisioning did not
#: complete. Deliberately distinct from 409 (idempotency semantics) so an MCP client can
#: tell "do not retry, escalate" apart from "retry the same key later".
HTTP_MANUAL_INTERVENTION = 424
