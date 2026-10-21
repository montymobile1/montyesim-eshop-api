"""Order amount -> LinkAja amount conversion.

BUSINESS DECISION STILL OPEN: orders are stored in USD minor units, while the
LinkAja collection only ever sends `"currency": "IDR"` with a decimal-string
value ("29880.00"). No exchange rate is invented here: the rate is the
`LINKAJA_USD_TO_IDR_RATE` setting, and conversion fails closed when it is unset
or invalid.

Rounding: the only amount in the docs is a whole-rupiah value written with two
decimals, and IDR has no minor unit in practice, so we round to whole rupiah
(half-up) and format with ".00". Neither the rounding mode nor whether decimals
are accepted is documented.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .exceptions import LinkAjaConfigurationException

LINKAJA_CURRENCY = "IDR"


def parse_usd_to_idr_rate(raw: str) -> Decimal:
    """Validate the configured rate. Unset, non-numeric or non-positive fails closed."""
    if not raw:
        raise LinkAjaConfigurationException(
            details="LinkAja amount conversion is not configured: LINKAJA_USD_TO_IDR_RATE is not set")
    try:
        rate = Decimal(raw)
    except InvalidOperation as exc:
        raise LinkAjaConfigurationException(
            details="LinkAja amount conversion is not configured: LINKAJA_USD_TO_IDR_RATE is not a number") from exc
    if not rate.is_finite() or rate <= 0:
        raise LinkAjaConfigurationException(
            details="LinkAja amount conversion is not configured: LINKAJA_USD_TO_IDR_RATE must be positive")
    return rate


def to_linkaja_amount(amount_minor_units: int, currency: str, usd_to_idr_rate: str) -> str:
    """Convert an order amount to LinkAja's IDR value string, e.g. 1099 USD cents -> "180236.00"."""
    if str(currency).upper() != "USD":
        raise LinkAjaConfigurationException(
            details=f"LinkAja amount conversion supports USD orders only, got {currency}")
    rate = parse_usd_to_idr_rate(usd_to_idr_rate)
    rupiah = (Decimal(int(amount_minor_units)) / Decimal(100) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if rupiah <= 0:
        raise LinkAjaConfigurationException(
            details="LinkAja amount conversion produced a non-positive amount")
    return f"{rupiah}.00"
