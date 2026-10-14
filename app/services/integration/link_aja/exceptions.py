"""Exception vocabulary for the LinkAja integration.

Every failure mode the integration can produce has a distinct, translatable
error name so the API layer never has to inspect exception messages. All of
these carry a `name` from `ErrorMessages`, which the global exception handler
renders through `locales/*.json`.

None of these exceptions ever carry provider credentials, tokens or raw
provider payloads in `details` - see `sanitize.py`.
"""

from app.config.constants import ErrorMessages
from app.exceptions import CustomException


class LinkAjaException(CustomException):
    """Base class for every LinkAja failure.

    Lets callers (and tests) catch the whole family without depending on which
    specific failure occurred.

    `snap_response_code` is what the notify route answers LinkAja with when the
    failure happens while handling an Inform Payment (spec §5.2: HTTP status +
    service code 56 + case code). Our own API clients never see it.
    """

    snap_response_code: str = "5005600"   # General Error
    snap_response_message: str = "General Error"


class LinkAjaDisabledException(LinkAjaException):
    """`payment_type="LinkAja"` arrived on a deployment with the flag off.

    This is a client-visible 400 rather than a server error: the request is
    well-formed, the deployment simply does not offer LinkAja.
    """

    # A notify for a deployment that does not offer LinkAja: nothing to retry.
    snap_response_code = "4005600"
    snap_response_message = "Bad Request"

    def __init__(self, details: str = "LinkAja payments are not enabled on this deployment"):
        super().__init__(code=400, name=ErrorMessages.LINKAJA_DISABLED, details=details)


class LinkAjaConfigurationException(LinkAjaException):
    """`LINKAJA_ENABLED=true` but mandatory configuration is missing or blank.

    Deliberately a 503: the deployment intends to offer LinkAja but is not
    correctly provisioned, which is an operator problem, not a client problem.
    Raised per-request so a misconfigured LinkAja never blocks startup or
    affects Card / Wallet / DCB.
    """

    # §5.2 "Internal Server Error - please retry": an operator problem that a
    # later retry from LinkAja may find fixed.
    snap_response_code = "5005601"
    snap_response_message = "Internal Server Error"

    def __init__(self, details: str):
        super().__init__(code=503, name=ErrorMessages.LINKAJA_CONFIGURATION_INVALID, details=details)


class LinkAjaNotConfiguredException(LinkAjaException):
    """A provider operation was attempted whose contract is not yet specified.

    This is the marker for "blocked on the official LinkAja specification".
    It is what keeps the integration honest: rather than guessing an endpoint,
    payload or signature, the operation fails loudly and safely.
    """

    snap_response_code = "5005601"
    snap_response_message = "Internal Server Error"

    def __init__(self, details: str):
        super().__init__(code=503, name=ErrorMessages.LINKAJA_NOT_CONFIGURED, details=details)


class LinkAjaProviderException(LinkAjaException):
    """LinkAja was unreachable, timed out, or returned an unusable response.

    502 because this is an upstream failure. The provider's raw response is
    never propagated to the client.
    """

    def __init__(self, details: str, status_code: int | None = None):
        super().__init__(code=502, name=ErrorMessages.LINKAJA_PROVIDER_ERROR, details=details)
        # LinkAja's HTTP status when there was one (e.g. 401 -> token expired).
        self.status_code = status_code


class LinkAjaCallbackVerificationException(LinkAjaException):
    """An inbound callback could not be authenticated.

    401 and always fail-closed: an unverified callback must never provision.
    """

    snap_response_code = "4015600"
    snap_response_message = "Unauthorized. Invalid signature"

    def __init__(self, details: str):
        super().__init__(code=401, name=ErrorMessages.LINKAJA_CALLBACK_UNVERIFIED, details=details)


class LinkAjaCallbackInvalidException(LinkAjaException):
    """An authenticated notify whose content we cannot accept: malformed body,
    missing mandatory field, wrong merchant or an amount that does not match
    the order. 400 to our API; the SNAP code is chosen per case (§5.2)."""

    def __init__(self, details: str, snap_response_code: str = "4005602",
                 snap_response_message: str = "Invalid Mandatory Field"):
        super().__init__(code=400, name=ErrorMessages.LINKAJA_CALLBACK_INVALID, details=details)
        self.snap_response_code = snap_response_code
        self.snap_response_message = snap_response_message


class LinkAjaOrderNotFoundException(LinkAjaException):
    """A verified callback referenced an order that does not exist here."""

    snap_response_code = "4045601"
    snap_response_message = "Transaction Not Found"

    def __init__(self, details: str):
        super().__init__(code=404, name=ErrorMessages.LINKAJA_ORDER_NOT_FOUND, details=details)
