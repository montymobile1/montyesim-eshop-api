"""Per-order LinkAja payment data, stored on the order without a migration.

What LinkAja is asked to collect is an IDR amount derived from the USD order
amount and `LINKAJA_USD_TO_IDR_RATE` at creation time. The notify must be
reconciled against *that* amount, not against a recomputation with whatever
the rate is when the notify arrives - otherwise a rate change while a payment
is in flight would reject a perfectly good notify.

`user_order` has no spare column, so the quote is embedded in `bundle_data`:
the order's JSON snapshot of the `BundleDTO` that was sold, under one reserved
key. This is safe because every reader of `user_order.bundle_data` parses it
with `BundleDTO.model_validate_json`, and `BundleDTO` has `extra="ignore"`, so
the extra key is invisible to them. Nothing in the database parses the column.

Caveat: `UserBundleService.assign` schedules a delayed rewrite of
`bundle_data` on the promo-code path (`__update_order_with_delay`). That task
never runs today (it builds the coroutine without awaiting it), but if it is
ever fixed it must preserve this key, or `embed_order_data` must be reapplied.
Reconciliation fails closed when the key is missing, so a lost quote can never
cause a wrong provisioning - only a rejected notify.
"""

import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

#: Reserved key inside the order's `bundle_data` JSON object.
ORDER_DATA_KEY = "linkaja_payment"


class LinkAjaOrderData(BaseModel):
    """The exact amount sent to LinkAja for an order, and how it was derived."""

    amount: str                    # IDR value string as sent, e.g. "175840.00"
    currency: str                  # "IDR"
    usd_to_idr_rate: str           # LINKAJA_USD_TO_IDR_RATE at creation
    order_amount_minor_units: int  # the USD cents the amount was derived from
    quoted_at: str                 # ISO timestamp, for support/debugging

    model_config = ConfigDict(extra="ignore")


def _load_object(bundle_data: Optional[str]) -> Optional[dict[str, Any]]:
    if not bundle_data:
        return None
    try:
        parsed = json.loads(bundle_data)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def embed_order_data(bundle_data: Optional[str], data: LinkAjaOrderData) -> str:
    """Return `bundle_data` with the quote embedded under `ORDER_DATA_KEY`.

    The bundle snapshot itself is left byte-for-byte equivalent (same keys,
    same values); only the reserved key is added or replaced. Raises
    `ValueError` if `bundle_data` is not a JSON object, because then the quote
    could not be read back and reconciliation would be impossible.
    """
    snapshot = _load_object(bundle_data)
    if snapshot is None:
        raise ValueError("order bundle_data is not a JSON object; cannot embed the LinkAja quote")
    snapshot[ORDER_DATA_KEY] = data.model_dump()
    return json.dumps(snapshot)


def read_order_data(bundle_data: Optional[str]) -> Optional[LinkAjaOrderData]:
    """The embedded quote, or None when absent or unreadable."""
    snapshot = _load_object(bundle_data)
    if not snapshot or not isinstance(snapshot.get(ORDER_DATA_KEY), dict):
        return None
    try:
        return LinkAjaOrderData.model_validate(snapshot[ORDER_DATA_KEY])
    except ValueError:
        return None
