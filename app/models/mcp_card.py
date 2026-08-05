"""Persistence models for the MCP Stripe hosted-checkout feature."""

from typing import Optional

from pydantic import BaseModel, ConfigDict


class McpCardCheckoutModel(BaseModel):
    """One MCP card checkout attempt.

    ``id`` is the ``payment_reference`` handed to the caller: an opaque uuid that is
    meaningless without ownership, so a leaked reference reveals nothing.

    No Stripe secret is modelled here. ``stripe_session_id`` and
    ``stripe_payment_intent_id`` are non-secret identifiers kept for reconciliation and
    are never returned to a caller.
    """

    id: Optional[str] = None
    user_id: str
    order_id: Optional[str] = None
    bundle_code: str
    quote_reference: Optional[str] = None
    idempotency_record_id: Optional[str] = None
    request_hash: Optional[str] = None
    #: Authoritative server-computed amount, in the currency's smallest unit.
    amount_minor: int
    currency: str
    status: str
    stripe_session_id: Optional[str] = None
    stripe_payment_intent_id: Optional[str] = None
    checkout_url: Optional[str] = None
    expires_at: Optional[str] = None
    paid_at: Optional[str] = None
    provisioned_at: Optional[str] = None
    failure_code: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class McpStripeWebhookEventModel(BaseModel):
    """A Stripe event this feature has already accepted.

    The primary key is Stripe's own event id, so a duplicate delivery collides on
    insert and is discarded before it can do anything.
    """

    id: Optional[str] = None
    event_id: str
    event_type: str
    checkout_id: Optional[str] = None
    status: Optional[str] = None
    received_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")
