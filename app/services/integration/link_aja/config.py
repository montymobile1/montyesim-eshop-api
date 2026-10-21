"""Configuration for the LinkAja integration.

Everything provider-specific that can vary between deployments lives here, and
is read from the environment **at call time, never at import time**. That is
what guarantees requirement "application startup MUST continue working when
LinkAja is not configured": importing this module cannot fail, cannot raise and
cannot log, regardless of what is or is not set in the environment.

Two categories of value are declared:

1. **Deployment values** - base URL, credentials, redirect URLs. Names are
   stable; only the values come from the LinkAja onboarding pack.
2. **Contract values** - endpoint paths and the notify (Inform Payment)
   verification inputs. These are declared as configuration so that a change
   on LinkAja's side can be applied without a code change. They default to
   empty, and an empty contract value makes the corresponding operation fail
   closed rather than issue a guessed request or accept an unverified notify.

Spec reference: "LinkAja Applink SNAP" v1.1 (2025-05-08), §4.3 Inform Payment
and §5.1.3 signature for debit notify.
"""

import os
from dataclasses import dataclass, field
from typing import List

from .exceptions import LinkAjaConfigurationException

TRUTHY = ("true", "1", "yes", "on")

# Applied to every outbound LinkAja call. Deliberately modest: a payment
# initiation sits in the user's request path.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0

# urlParams[].isDeeplink: our notify URL is a plain web URL, not an app deeplink.
DEFAULT_IS_DEEPLINK = "N"

# EndpointUrl forms of the notify string-to-sign (§5.1.3) that can be tried.
# The spec does not say which one LinkAja signs, and our urlParams URL carries
# a query string (?order_id=...):
#   path        /api/v1/callback/linkaja
#   path_query  /api/v1/callback/linkaja?order_id=...
#   full_url    https://<host>/api/v1/callback/linkaja?order_id=...
NOTIFY_ENDPOINT_FORMS = ("path", "path_query", "full_url")
DEFAULT_NOTIFY_SIGNATURE_FORMS = "path"


def _env(name: str, default: str = "") -> str:
    """Read an env var, treating whitespace-only as absent."""
    return (os.getenv(name, default) or "").strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        # An unparseable timeout must not take the deployment down; fall back to
        # the safe default. The value is non-secret, so it is safe to surface.
        return default


def is_link_aja_enabled() -> bool:
    """Fail-closed feature gate for the LinkAja payment type.

    Read at call time so the flag can be flipped on a Render deployment without
    a code change.

    This flag does NOT select the payment method - the frontend does that via
    `payment_type`. It only decides whether a `payment_type="LinkAja"` request
    is allowed to execute on this deployment. Defaults to false, which is what
    keeps the shared QA deployment safe for resellers that have not onboarded
    LinkAja.
    """
    return _env("LINKAJA_ENABLED", "false").lower() in TRUTHY


@dataclass(frozen=True)
class LinkAjaConfig:
    """Immutable snapshot of the LinkAja configuration for one operation."""

    enabled: bool = False

    # --- deployment values (names stable, values from onboarding) ----------
    base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    merchant_id: str = ""
    # SNAP headers/body values from the LinkAja onboarding pack.
    partner_id: str = ""        # X-PARTNER-ID
    channel_id: str = ""        # CHANNEL-ID
    terminal_id: str = ""       # body terminalId / additionalInfo.terminalId
    terminal_name: str = ""     # body additionalInfo.terminalName
    # RSA key that signs the access-token request: PEM text, or a path to it.
    private_key: str = ""
    private_key_path: str = ""
    # Sent as User-Agent on payment creation (the collection sends "ios").
    user_agent: str = ""
    # urlParams[].isDeeplink on payment creation (spec p.11: "Y"/"N").
    url_param_is_deeplink: str = DEFAULT_IS_DEEPLINK
    # USD -> IDR rate. BUSINESS DECISION: see amount.py. Blank fails closed.
    usd_to_idr_rate: str = ""
    # Public URL of our notify endpoint. LinkAja calls it after the payment, and
    # it is sent to them as urlParams[0].url (type PAY_RETURN) with
    # "?order_id=<order id>" appended. LinkAja uses the same URL to send the
    # customer back (a GET), which redirects to frontend_return_url.
    notify_url: str = ""
    # Frontend page the customer lands on after paying, e.g.
    # "https://<frontend>/plans?order_id={order_id}". `{order_id}` is replaced
    # with the order id (or removed when the GET has no valid one).
    frontend_return_url: str = ""
    # Not sent to LinkAja by any request; kept for reference only.
    callback_url: str = ""
    success_url: str = ""
    cancel_url: str = ""

    # --- contract values (filled in from the official specification) -------
    # Empty means "not specified yet" -> the operation fails closed instead of
    # issuing a guessed request. These are configuration rather than constants
    # so applying the spec does not require touching Python at all.
    auth_path: str = ""
    create_payment_path: str = ""
    payment_status_path: str = ""
    # LinkAja's public key that verifies the notify X-SIGNATURE (spec §5.1.3,
    # SHA256withRSA): PEM text, or a path to it. Unset = every notify is rejected.
    notify_public_key: str = ""
    notify_public_key_path: str = ""
    # The "EndpointUrl" LinkAja puts in the notify stringToSign. Not pinned by
    # the spec (full URL or path?); when empty, it is built from the request as
    # received, in the forms listed by notify_signature_forms.
    notify_endpoint_url: str = ""
    # Comma-separated EndpointUrl forms tried, in order, when
    # notify_endpoint_url is empty: "path", "path_query", "full_url" (see
    # NOTIFY_ENDPOINT_FORMS). Default: the path without the query string.
    notify_signature_forms: str = DEFAULT_NOTIFY_SIGNATURE_FORMS

    # --- transport --------------------------------------------------------
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT

    @classmethod
    def from_env(cls) -> "LinkAjaConfig":
        """Build a config snapshot from the current environment.

        Never raises: an unconfigured environment yields a config whose
        `enabled` is False and whose values are empty. Validation is a separate,
        explicit step so that merely constructing the service is always safe.
        """
        return cls(
            enabled=is_link_aja_enabled(),
            base_url=_env("LINKAJA_BASE_URL"),
            client_id=_env("LINKAJA_CLIENT_ID"),
            client_secret=_env("LINKAJA_CLIENT_SECRET"),
            merchant_id=_env("LINKAJA_MERCHANT_ID"),
            partner_id=_env("LINKAJA_PARTNER_ID"),
            channel_id=_env("LINKAJA_CHANNEL_ID"),
            terminal_id=_env("LINKAJA_TERMINAL_ID"),
            terminal_name=_env("LINKAJA_TERMINAL_NAME"),
            private_key=_env("LINKAJA_PRIVATE_KEY"),
            private_key_path=_env("LINKAJA_PRIVATE_KEY_PATH"),
            user_agent=_env("LINKAJA_USER_AGENT"),
            url_param_is_deeplink=_env("LINKAJA_URL_PARAM_IS_DEEPLINK", DEFAULT_IS_DEEPLINK) or DEFAULT_IS_DEEPLINK,
            usd_to_idr_rate=_env("LINKAJA_USD_TO_IDR_RATE"),
            notify_url=_env("LINKAJA_NOTIFY_URL"),
            frontend_return_url=_env("LINKAJA_FRONTEND_RETURN_URL"),
            callback_url=_env("LINKAJA_CALLBACK_URL"),
            success_url=_env("LINKAJA_SUCCESS_URL"),
            cancel_url=_env("LINKAJA_CANCEL_URL"),
            auth_path=_env("LINKAJA_AUTH_PATH"),
            create_payment_path=_env("LINKAJA_CREATE_PAYMENT_PATH"),
            payment_status_path=_env("LINKAJA_PAYMENT_STATUS_PATH"),
            notify_public_key=_env("LINKAJA_NOTIFY_PUBLIC_KEY"),
            notify_public_key_path=_env("LINKAJA_NOTIFY_PUBLIC_KEY_PATH"),
            notify_endpoint_url=_env("LINKAJA_NOTIFY_ENDPOINT_URL"),
            notify_signature_forms=(_env("LINKAJA_NOTIFY_SIGNATURE_FORMS", DEFAULT_NOTIFY_SIGNATURE_FORMS)
                                    or DEFAULT_NOTIFY_SIGNATURE_FORMS),
            connect_timeout=_env_float("LINKAJA_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT),
            read_timeout=_env_float("LINKAJA_READ_TIMEOUT", DEFAULT_READ_TIMEOUT),
        )

    # -- validation --------------------------------------------------------
    # Split by operation so a deployment that can create payments is not blocked
    # by, say, a missing status-query path.

    def missing_payment_settings(self) -> List[str]:
        """Deployment values required before any payment can be initiated.

        LINKAJA_NOTIFY_URL is the `urlParams` URL: LinkAja confirmed that our notify
        endpoint goes there. LINKAJA_SUCCESS_URL, LINKAJA_CALLBACK_URL and
        LINKAJA_CANCEL_URL are not required: no LinkAja request carries them.
        """
        required = {
            "LINKAJA_BASE_URL": self.base_url,
            "LINKAJA_CLIENT_ID": self.client_id,
            "LINKAJA_CLIENT_SECRET": self.client_secret,
            "LINKAJA_PARTNER_ID": self.partner_id,
            "LINKAJA_CHANNEL_ID": self.channel_id,
            "LINKAJA_MERCHANT_ID": self.merchant_id,
            "LINKAJA_TERMINAL_ID": self.terminal_id,
            "LINKAJA_TERMINAL_NAME": self.terminal_name,
            "LINKAJA_PRIVATE_KEY or LINKAJA_PRIVATE_KEY_PATH": self.private_key or self.private_key_path,
            "LINKAJA_USD_TO_IDR_RATE": self.usd_to_idr_rate,
            "LINKAJA_NOTIFY_URL": self.notify_url,
        }
        return sorted(name for name, value in required.items() if not value)

    def validate_for_payment(self) -> None:
        """Raise a descriptive configuration error if payments cannot be made.

        Names only - never values - so this can be logged and returned safely.
        """
        missing = self.missing_payment_settings()
        if missing:
            raise LinkAjaConfigurationException(
                details=f"LinkAja is enabled but required configuration is missing: {', '.join(missing)}"
            )

    def validate_for_callback(self) -> None:
        """Raise if inbound notifies cannot be authenticated.

        The notify is signed with SHA256withRSA (spec §5.1.3), so the one thing
        that can authenticate it is LinkAja's public key. Without it every
        notify is rejected: this is checked independently of payment creation
        and fails closed.
        """
        if not (self.notify_public_key or self.notify_public_key_path):
            raise LinkAjaConfigurationException(
                details="LinkAja callback verification is not configured: "
                        "LINKAJA_NOTIFY_PUBLIC_KEY or LINKAJA_NOTIFY_PUBLIC_KEY_PATH"
            )
        self.signature_forms()

    def signature_forms(self) -> List[str]:
        """The EndpointUrl forms to try, in order. An unknown or empty list is a
        configuration error (fails closed) rather than a silent default."""
        forms = [f.strip().lower() for f in (self.notify_signature_forms or "").split(",") if f.strip()]
        unknown = [f for f in forms if f not in NOTIFY_ENDPOINT_FORMS]
        if not forms or unknown:
            raise LinkAjaConfigurationException(
                details=f"LINKAJA_NOTIFY_SIGNATURE_FORMS must list one or more of "
                        f"{', '.join(NOTIFY_ENDPOINT_FORMS)}")
        return list(dict.fromkeys(forms))


def link_aja_config() -> LinkAjaConfig:
    """Convenience accessor mirroring the project's other config helpers."""
    return LinkAjaConfig.from_env()
