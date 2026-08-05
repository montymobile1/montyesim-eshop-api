"""Repositories for the MCP Stripe hosted-checkout feature.

Ownership is expressed in the query, never checked after the fact: every read is
scoped to ``user_id`` so a cross-user read is impossible by construction rather than
by a forgotten ``if``.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.exceptions import DatabaseException
from app.models.mcp_card import McpCardCheckoutModel, McpStripeWebhookEventModel
from app.repo.base_repo import BaseRepository


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class McpCardCheckoutRepo(BaseRepository):

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_MCP_CARD_CHECKOUT, McpCardCheckoutModel)

    def get_for_user(self, checkout_id: str, user_id: str) -> Optional[McpCardCheckoutModel]:
        """Fetch scoped to the owner. A foreign reference returns ``None``, exactly like
        a reference that never existed - the caller cannot tell the two apart."""
        return self.get_first_by(where={"id": checkout_id, "user_id": user_id})

    def get_by_session_id(self, session_id: str) -> Optional[McpCardCheckoutModel]:
        """Webhook-side lookup. Not user scoped: the webhook has no authenticated user,
        it is trusted only because its signature was verified."""
        return self.get_first_by(where={"stripe_session_id": session_id})

    def get_by_payment_intent_id(self, payment_intent_id: str) -> Optional[McpCardCheckoutModel]:
        """Primary webhook-side lookup: the events we consume are ``payment_intent.*``,
        so the intent id is what identifies the checkout."""
        return self.get_first_by(where={"stripe_payment_intent_id": payment_intent_id})

    def get_by_record_id(self, idempotency_record_id: str, user_id: str) -> Optional[McpCardCheckoutModel]:
        return self.get_first_by(where={"idempotency_record_id": idempotency_record_id,
                                        "user_id": user_id})

    def attach_session(self, checkout_id: str, session_id: str, checkout_url: str,
                       expires_at: Optional[str], payment_intent_id: Optional[str] = None):
        data: Dict[str, Any] = {
            "stripe_session_id": session_id,
            "checkout_url": checkout_url,
            "expires_at": expires_at,
            "updated_at": _now(),
        }
        if payment_intent_id:
            data["stripe_payment_intent_id"] = payment_intent_id
        return self.update_by(where={"id": checkout_id}, data=data)

    def attach_order(self, checkout_id: str, order_id: str):
        return self.update_by(where={"id": checkout_id},
                              data={"order_id": order_id, "updated_at": _now()})

    def transition(self, checkout_id: str, from_statuses: List[str], to_status: str,
                   extra: Optional[Dict[str, Any]] = None) -> bool:
        """Compare-and-set the status. Returns True only for the caller that won.

        One SQL ``UPDATE ... WHERE id = ? AND status = ?`` per candidate state. Postgres
        locks the row for the duration of each statement, so of two concurrent workers
        exactly one can observe a matching row and the loser updates nothing. This is
        what makes provisioning happen at most once without any application lock.
        """
        for from_status in from_statuses:
            data: Dict[str, Any] = {"status": to_status, "updated_at": _now()}
            if extra:
                data.update(extra)
            try:
                updated = self.update_by(where={"id": checkout_id, "status": from_status}, data=data)
            except DatabaseException:
                raise
            if updated:
                return True
        return False


class McpStripeWebhookEventRepo(BaseRepository):
    """Dedupe ledger for Stripe events consumed by the MCP card flow.

    The legacy webhook has never de-duplicated events; this ledger is scoped to MCP
    card events only, so legacy behaviour is untouched.
    """

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_MCP_STRIPE_WEBHOOK_EVENT, McpStripeWebhookEventModel)

    def claim(self, event_id: str, event_type: str, checkout_id: Optional[str] = None) -> bool:
        """Record the event. Returns False when it was already recorded (a duplicate).

        Relies on the primary-key/unique constraint on ``event_id``: the second insert
        raises and is swallowed, which is the whole dedupe mechanism.
        """
        try:
            self.create({"event_id": event_id, "event_type": event_type,
                         "checkout_id": checkout_id, "received_at": _now()})
            return True
        except Exception:
            # Unique violation => this exact event was already accepted. Any other
            # failure is also treated as "do not process twice": refusing to act is
            # always safe here, because Stripe will redeliver.
            return False

    def mark_result(self, event_id: str, status: str):
        try:
            return self.update_by(where={"event_id": event_id}, data={"status": status})
        except Exception:
            return None
