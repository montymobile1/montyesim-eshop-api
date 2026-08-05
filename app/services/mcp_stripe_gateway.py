"""The only place the MCP card feature talks to Stripe.

Everything Stripe-shaped is confined here so that:
  * the purchase service can be tested with a fake gateway and never imports ``stripe``;
  * no Stripe object, error string or raw payload can leak upward - callers receive a
    small dataclass or a typed internal error;
  * secrets have exactly one code path and are never logged.

This module does NOT touch ``stripe.api_key``: it is already configured once, at import
of ``app.config.utils``, by the legacy integration. Re-assigning it here could change
legacy behaviour, so we deliberately read the same configuration and pass the key
explicitly per call instead.
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import stripe
from loguru import logger

from app.config.mcp_card_constants import (
    CARD_CANCEL_URL_ENV,
    CARD_SESSION_EXPIRY_MINUTES_ENV,
    CARD_SUCCESS_URL_ENV,
    DEFAULT_SESSION_EXPIRY_MINUTES,
    MAX_SESSION_EXPIRY_MINUTES,
    MIN_SESSION_EXPIRY_MINUTES,
    STRIPE_MODE,
    STRIPE_PAYMENT_METHOD_TYPES,
    McpCardErrorMessages,
)
from app.exceptions import CustomException

#: Local/test hosts where a plain-http redirect target is tolerable.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

#: Anything that looks like a Stripe credential, so it can be scrubbed from any text
#: that might reach a log line. Covers secret keys, restricted keys, webhook secrets,
#: session/intent client secrets and publishable keys.
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|rk)_(live|test)_[A-Za-z0-9]+"),
    re.compile(r"\bwhsec_[A-Za-z0-9]+"),
    re.compile(r"\bpk_(live|test)_[A-Za-z0-9]+"),
    re.compile(r"\b(cs|pi|seti)_[A-Za-z0-9]+_secret_[A-Za-z0-9]+"),
]


def redact(text: Any) -> str:
    """Return ``text`` with anything credential-shaped replaced by a marker.

    Used on every string this module is willing to log or attach to an error.
    """
    rendered = str(text)
    for pattern in _SECRET_PATTERNS:
        rendered = pattern.sub("[REDACTED]", rendered)
    return rendered


class StripeGatewayError(Exception):
    """Internal, typed failure. Carries a category, never a Stripe payload."""

    def __init__(self, category: str, message: str = "", retryable: bool = False,
                 ambiguous: bool = False):
        super().__init__(category)
        self.category = category
        self.message = redact(message)
        self.retryable = retryable
        #: True when we cannot know whether Stripe created the Session (timeout / API
        #: connection error). The caller must reconcile, never blindly retry.
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class CheckoutSessionResult:
    """The only Stripe-derived data allowed to leave this module."""

    session_id: str
    checkout_url: Optional[str]
    payment_intent_id: Optional[str]
    amount_total_minor: Optional[int]
    currency: Optional[str]
    expires_at: Optional[str]
    status: Optional[str]
    payment_status: Optional[str]


def _validated_url(env_name: str) -> str:
    """Read and validate a redirect target from configuration.

    HTTPS is mandatory outside local/test hosts. A misconfigured URL fails closed with
    a config error rather than producing a Session that redirects somewhere unsafe.
    """
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        raise CustomException(
            code=503, name=McpCardErrorMessages.MCP_CARD_CONFIG_INVALID,
            details=f"{env_name} is not configured")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CustomException(
            code=503, name=McpCardErrorMessages.MCP_CARD_CONFIG_INVALID,
            details=f"{env_name} must be an absolute http(s) URL")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in _LOCAL_HOSTS:
        raise CustomException(
            code=503, name=McpCardErrorMessages.MCP_CARD_CONFIG_INVALID,
            details=f"{env_name} must use HTTPS outside local environments")
    return raw


def success_url() -> str:
    return _validated_url(CARD_SUCCESS_URL_ENV)


def cancel_url() -> str:
    return _validated_url(CARD_CANCEL_URL_ENV)


def session_expiry_minutes() -> int:
    """Bounded session lifetime, clamped to what Stripe accepts."""
    try:
        minutes = int(os.getenv(CARD_SESSION_EXPIRY_MINUTES_ENV, DEFAULT_SESSION_EXPIRY_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_SESSION_EXPIRY_MINUTES
    return max(MIN_SESSION_EXPIRY_MINUTES, min(MAX_SESSION_EXPIRY_MINUTES, minutes))


def _api_key() -> str:
    key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        raise CustomException(code=503, name=McpCardErrorMessages.MCP_CARD_CONFIG_INVALID,
                              details="Stripe is not configured")
    return key


def is_test_mode() -> bool:
    """True when the configured secret key is a Stripe *test* key."""
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip().startswith(("sk_test_", "rk_test_"))


def _iso(epoch: Optional[int]) -> Optional[str]:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _as_result(session: Any) -> CheckoutSessionResult:
    """Project a Stripe Session down to the handful of non-secret fields we keep."""
    def field(name):
        if isinstance(session, dict):
            return session.get(name)
        return getattr(session, name, None)

    payment_intent = field("payment_intent")
    if payment_intent is not None and not isinstance(payment_intent, str):
        payment_intent = (payment_intent.get("id") if isinstance(payment_intent, dict)
                          else getattr(payment_intent, "id", None))

    return CheckoutSessionResult(
        session_id=field("id"),
        checkout_url=field("url"),
        payment_intent_id=payment_intent,
        amount_total_minor=field("amount_total"),
        currency=(field("currency") or None),
        expires_at=_iso(field("expires_at")),
        status=field("status"),
        payment_status=field("payment_status"),
    )


class McpStripeCheckoutGateway:
    """Thin, mockable wrapper over Stripe Checkout Sessions."""

    def create_checkout_session(self, *, amount_minor: int, currency: str, product_name: str,
                                metadata: Dict[str, str], idempotency_key: str,
                                client_reference_id: str,
                                customer_email: Optional[str] = None) -> CheckoutSessionResult:
        """Create exactly one hosted Checkout Session.

        ``idempotency_key`` is the *same stable identity* used for backend persistence,
        so a retried call after a timeout returns Stripe's original Session instead of
        creating a second one.

        ``metadata`` is stamped on BOTH the Session and, via ``payment_intent_data``, on
        the PaymentIntent the Session creates. That is what makes this flow observable
        through the very same ``payment_intent.succeeded`` event the legacy Card flow
        uses, carrying the very same metadata payload. The ``mcp_source`` marker inside
        it is what routes the event to this feature rather than to the legacy handler.
        """
        expires_at = int((datetime.now(tz=timezone.utc)
                          + timedelta(minutes=session_expiry_minutes())).timestamp())
        try:
            session = stripe.checkout.Session.create(
                api_key=_api_key(),
                mode=STRIPE_MODE,
                payment_method_types=STRIPE_PAYMENT_METHOD_TYPES,
                success_url=success_url(),
                cancel_url=cancel_url(),
                client_reference_id=client_reference_id,
                customer_email=customer_email or None,
                expires_at=expires_at,
                line_items=[{
                    "quantity": 1,
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": int(amount_minor),
                        "product_data": {"name": product_name},
                    },
                }],
                metadata=metadata,
                # Same payload on the PaymentIntent, so payment_intent.succeeded carries
                # everything the legacy handler's contract expects plus our marker.
                payment_intent_data={"metadata": metadata},
                idempotency_key=idempotency_key,
            )
            return _as_result(session)
        except stripe.error.CardError as e:
            raise StripeGatewayError("CARD_DECLINED", str(e))
        except stripe.error.IdempotencyError as e:
            # Same key replayed with different parameters: never silently create another.
            raise StripeGatewayError("IDEMPOTENCY_MISMATCH", str(e))
        except stripe.error.RateLimitError as e:
            raise StripeGatewayError("RATE_LIMITED", str(e), retryable=True)
        except (stripe.error.APIConnectionError, stripe.error.APIError) as e:
            # We cannot tell whether the Session was created. Reconcile, do not recreate.
            raise StripeGatewayError("UNKNOWN", str(e), retryable=True, ambiguous=True)
        except stripe.error.StripeError as e:
            raise StripeGatewayError("STRIPE_ERROR", str(e))
        except Exception as e:  # never let a raw provider error escape
            logger.error(f"mcp.card stripe session creation failed: {redact(e)}")
            raise StripeGatewayError("UNKNOWN", str(e), ambiguous=True)

    def retrieve_session(self, session_id: str) -> Optional[CheckoutSessionResult]:
        """Read back a Session for reconciliation. Never raises to the caller."""
        try:
            session = stripe.checkout.Session.retrieve(session_id, api_key=_api_key())
            return _as_result(session)
        except Exception as e:
            logger.warning(f"mcp.card could not retrieve session: {redact(e)}")
            return None

    def expire_session(self, session_id: str) -> Optional[CheckoutSessionResult]:
        try:
            session = stripe.checkout.Session.expire(session_id, api_key=_api_key())
            return _as_result(session)
        except Exception as e:
            logger.warning(f"mcp.card could not expire session: {redact(e)}")
            return None
