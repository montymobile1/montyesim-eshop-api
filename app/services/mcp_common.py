"""Validation and translation helpers shared by the MCP endpoints.

Everything here is pure: authentication checks, feature-flag gates, currency resolution
and payload translation. Nothing in this module reads or writes any database table, calls
any external service, or is imported by a legacy module.

The MCP endpoints deliberately own no persistence. Order, payment and provisioning state
all live in the existing ``user_order`` / ``user_profile`` rows written by the shared
purchase flow and by the existing webhook, and are read back from there.
"""

import os
from typing import Optional

from app.config.constants import ErrorMessages, PaymentStatusEnum
from app.config.db import OrderStatusEnum
from app.config.feature_flags import mcp_card_purchase_enabled, mcp_purchase_enabled
from app.config.mcp_constants import McpErrorMessages
from app.exceptions import CustomException
from app.models.user import UserModel
from app.schemas.bundle import CountryRequestDto, RegionRequestDto, RelatedSearchRequestDto
from app.schemas.mcp import McpRelatedSearchRequest


def system_currency() -> str:
    """The single currency MCP settles in, using the project-wide convention."""
    return os.getenv("SYSTEM_CURRENCY", "USD")


def effective_currency(x_currency: Optional[str]) -> str:
    """Resolve ``X-Currency``. Absent or blank means "the system currency".

    Deliberately not defaulted to ``DEFAULT_CURRENCY``: that is a presentation default and
    has never priced a purchase.
    """
    return (x_currency or "").strip().upper() or system_currency()


def ensure_supported_currency(x_currency: Optional[str]) -> str:
    """Reject a currency these endpoints cannot settle, before anything is created.

    The order row is written in the system currency, so silently accepting an arbitrary
    ``X-Currency`` would be a no-op an MCP client could reasonably mistake for a priced
    quote -- and a silent currency substitution means charging an amount the user was
    never told.
    """
    supported = system_currency()
    resolved = effective_currency(x_currency)
    if resolved != supported:
        raise CustomException(
            code=400,
            name=McpErrorMessages.MCP_UNSUPPORTED_CURRENCY,
            details=f"MCP purchases are settled in {supported} only. Send X-Currency: {supported}, or omit it.",
        )
    return resolved


def ensure_authenticated_user(user: Optional[UserModel]) -> UserModel:
    """MCP endpoints are for signed-in users only.

    The caller has already been resolved from a verified bearer token by the existing
    ``bearer_token`` dependency; this is the second half of the rule -- an anonymous or
    guest session is refused, and no user identity is ever taken from the request body.
    """
    if user is None or not getattr(user, "id", None):
        raise CustomException(
            code=401,
            name=ErrorMessages.BEARER_TOKEN_REQUIRED,
            details="An authenticated user is required for this operation",
        )
    if getattr(user, "is_anonymous", False):
        raise CustomException(
            code=401,
            name=McpErrorMessages.MCP_ANONYMOUS_NOT_ALLOWED,
            details="Anonymous sessions cannot use the MCP purchase endpoints",
        )
    return user


def ensure_mcp_purchase_enabled() -> None:
    """Gate every MCP purchase endpoint. Fails closed."""
    if not mcp_purchase_enabled():
        raise CustomException(
            code=503,
            name=McpErrorMessages.MCP_PURCHASE_DISABLED,
            details="MCP purchasing is not enabled on this deployment",
        )


def ensure_mcp_card_purchase_enabled() -> None:
    """Gate the MCP card endpoints. Fails closed, and requires the master flag too."""
    ensure_mcp_purchase_enabled()
    if not mcp_card_purchase_enabled():
        raise CustomException(
            code=503,
            name=McpErrorMessages.MCP_CARD_PURCHASE_DISABLED,
            details="MCP card checkout is not enabled on this deployment",
        )


def to_related_search(related: Optional[McpRelatedSearchRequest]) -> RelatedSearchRequestDto:
    """Translate the MCP related-search payload into the internal DTO.

    Pure shape translation, and never ``None``: the shared purchase flow serializes this
    value onto the order unconditionally, so it always receives the same
    ``RelatedSearchRequestDto`` a mobile or web request gives it.
    """
    region = None
    countries = None
    if related is not None:
        if related.region is not None:
            region = RegionRequestDto(iso_code=related.region.iso_code, region_name=related.region.region_name)
        if related.countries is not None:
            countries = [
                CountryRequestDto(iso3_code=country.iso3_code, country_name=country.country_name)
                for country in related.countries
            ]
    return RelatedSearchRequestDto(region=region, countries=countries)


def order_is_paid(payment_status: Optional[str]) -> bool:
    """True only where the order row itself records a successful payment."""
    return _normalized(payment_status) in (
        _normalized(OrderStatusEnum.SUCCESS),
        _normalized(PaymentStatusEnum.COMPLETED),
    )


def order_is_failed(payment_status: Optional[str]) -> bool:
    return _normalized(payment_status) == _normalized(OrderStatusEnum.FAILURE)


def order_is_cancelled(status: Optional[str]) -> bool:
    return _normalized(status) == _normalized(OrderStatusEnum.CANCELED)


def order_succeeded(order_status: Optional[str]) -> bool:
    return _normalized(order_status) == _normalized(OrderStatusEnum.SUCCESS)


def order_did_not_succeed(order_status: Optional[str]) -> bool:
    return _normalized(order_status) == _normalized(OrderStatusEnum.FAILURE)


def minor_units_to_decimal_string(minor_units: Optional[float]) -> Optional[str]:
    """Render a stored cent amount as a plain two-decimal string, or ``None``.

    A string rather than a float so a binary-rounding artefact can never be shown to a
    user as a price.
    """
    if minor_units is None:
        return None
    return f"{(round(float(minor_units)) / 100):.2f}"


def _normalized(value) -> str:
    return str(value or "").strip().lower()
