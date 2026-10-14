"""Internal DTOs for the LinkAja integration.

These are **our** models, not LinkAja's. Field names are deliberately neutral
and chosen by us; the mapping from LinkAja's JSON (spec v1.1 §4.3 / §4.4) onto
them lives in `service.py`, so consumers never see provider field names.

None of these models is ever returned through the public API. `UserBundleService`
maps the parts it needs onto `PaymentIntentResponse`; raw provider payloads never
leave the integration layer.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class LinkAjaPaymentStatus(StrEnum):
    """Normalized payment status used everywhere outside this package.

    LinkAja's `latestTransactionStatus` codes (spec §5.4) are mapped onto this
    fixed set at the integration boundary (see `map_provider_status`). Only
    `SUCCESS` is ever allowed to trigger provisioning; everything else - including
    `UNKNOWN` - is non-provisioning by construction.
    """

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    # §5.4 "04 Refunded": money went back to the customer. Never provisions.
    REFUNDED = "REFUNDED"
    # §5.4 "07 Not Found" (and inquiry responseCode 4045501): LinkAja has no
    # completed transaction for our reference, which for a freshly created
    # payment simply means "not paid yet". Leaves the order pending.
    NOT_FOUND = "NOT_FOUND"
    # Not a provider state: our marker for "the provider said something we have
    # no documented mapping for". Treated as non-provisioning and worth alerting on.
    UNKNOWN = "UNKNOWN"


#: The only statuses that may result in an eSIM being provisioned.
PROVISIONING_STATUSES = frozenset({LinkAjaPaymentStatus.SUCCESS})

#: `latestTransactionStatus` -> normalized status. Spec v1.1 §5.4 "List of
#: transaction status", the complete documented vocabulary:
#:
#:   00 Success    - transaction completed      -> the only provisioning status
#:   04 Refunded   - successfully refunded      -> never provisions
#:   06 Failed     - transaction failed
#:   07 Not Found  - transaction not found      -> order stays pending
#:
#: Anything else is undocumented and maps to UNKNOWN (non-provisioning).
PROVIDER_STATUS_MAP: dict[str, LinkAjaPaymentStatus] = {
    "00": LinkAjaPaymentStatus.SUCCESS,
    "04": LinkAjaPaymentStatus.REFUNDED,
    "06": LinkAjaPaymentStatus.FAILED,
    "07": LinkAjaPaymentStatus.NOT_FOUND,
}

#: Statuses that mean "LinkAja has not (yet) settled this payment": the order is
#: left pending so a later notify or inquiry can still settle it.
NON_FINAL_STATUSES = frozenset({LinkAjaPaymentStatus.PENDING, LinkAjaPaymentStatus.NOT_FOUND,
                                LinkAjaPaymentStatus.UNKNOWN})


def map_provider_status(provider_status: Optional[str]) -> LinkAjaPaymentStatus:
    """Map a raw provider status onto our normalized vocabulary.

    Unknown or unmapped values deliberately return `UNKNOWN` rather than
    guessing, and `UNKNOWN` never provisions.
    """
    if not provider_status:
        return LinkAjaPaymentStatus.UNKNOWN
    return PROVIDER_STATUS_MAP.get(str(provider_status).strip().upper(), LinkAjaPaymentStatus.UNKNOWN)


def is_provisioning_status(status: LinkAjaPaymentStatus) -> bool:
    """Single chokepoint for "may this status create an eSIM?"."""
    return status in PROVISIONING_STATUSES


class LinkAjaPaymentRequest(BaseModel):
    """What we ask LinkAja to create. Neutral, provider-agnostic field names."""

    # Our internal reference. This is `user_order.id` and is what correlates a
    # provider transaction back to an order.
    merchant_order_id: str
    amount_minor_units: int
    currency: str
    description: str = ""
    # Our notify endpoint, sent as urlParams[0].url. Sourced from configuration
    # rather than built here so it is deployment-controlled.
    notify_url: str = ""
    # Not sent to LinkAja; kept so the request records the deployment's URLs.
    callback_url: str = ""
    success_url: str = ""
    cancel_url: str = ""

    model_config = ConfigDict(extra="forbid")


class LinkAjaPaymentResult(BaseModel):
    """Outcome of a payment initiation.

    `provider_reference` is persisted onto `user_order.payment_intent_code`;
    `payment_url` is the only field surfaced to the frontend.
    """

    provider_reference: str
    payment_url: Optional[str] = None
    status: LinkAjaPaymentStatus = LinkAjaPaymentStatus.PENDING
    # The IDR amount string actually sent, and the rate it came from.
    amount: str = ""
    usd_to_idr_rate: str = ""

    model_config = ConfigDict(extra="forbid")


class LinkAjaStatusResult(BaseModel):
    """Outcome of an out-of-band transaction status query (spec §4.4)."""

    # LinkAja's `originalReferenceNo`; empty when LinkAja has no transaction.
    provider_reference: str = ""
    status: LinkAjaPaymentStatus = LinkAjaPaymentStatus.UNKNOWN
    merchant_order_id: Optional[str] = None
    # Raw `latestTransactionStatus` / `transactionStatusDesc` / `approvalCode`,
    # kept for logging and for a later refund (approvalCode is the refund key).
    provider_status: Optional[str] = None
    status_description: Optional[str] = None
    approval_code: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class LinkAjaCallbackItem(BaseModel):
    """One `additionalInfo.items[]` entry of an Inform Payment (spec p.15)."""

    id: str = ""
    name: str = ""
    unit_price: Optional[Decimal] = None
    qty: str = ""

    model_config = ConfigDict(extra="forbid")


class LinkAjaCallbackResult(BaseModel):
    """A parsed and authenticated inbound Inform Payment (spec §4.3, p.14-16).

    `verified` is not decoration: `LinkAjaCallbackService` refuses to provision
    unless it is True, so a parsed-but-unauthenticated callback cannot be
    mistaken for a trustworthy one.
    """

    merchant_order_id: str                       # originalPartnerReferenceNo
    provider_reference: Optional[str] = None     # originalReferenceNo
    status: LinkAjaPaymentStatus = LinkAjaPaymentStatus.UNKNOWN
    verified: bool = False
    provider_status: Optional[str] = None        # raw latestTransactionStatus
    merchant_id: Optional[str] = None
    # `amount.value` as a Decimal so "6000" and "6000.00" compare equal.
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    created_time: Optional[str] = None
    finished_time: Optional[str] = None
    # additionalInfo. approvalCode is echoed back in our 2005600 response.
    approval_code: Optional[str] = None
    terminal_id: Optional[str] = None
    terminal_name: Optional[str] = None
    items: list[LinkAjaCallbackItem] = []

    model_config = ConfigDict(extra="forbid")


class LinkAjaNotifyAck(BaseModel):
    """What the notify route answers LinkAja with on success (spec p.15-16):
    exactly `{"responseCode":"2005600","responseMessage":"Successful","approvalCode":…}`."""

    approval_code: str
    # True when provisioning was scheduled by this notify; False for duplicates
    # and non-success statuses. For logs/tests only - never sent to LinkAja.
    provisioning_scheduled: bool = False

    model_config = ConfigDict(extra="forbid")
