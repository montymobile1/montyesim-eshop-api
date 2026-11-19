from typing import Optional

from pydantic import BaseModel, ConfigDict


class UserOrderRefundModel(BaseModel):
    """One row of `user_order_refund`: the refund (or manual review) of a paid order whose eSIM Hub
    call failed. Kept out of `user_order` so the purchase flow's reads and writes stay unchanged."""
    order_id: str
    user_id: str
    payment_type: str
    status: str
    reason: Optional[str] = None
    amount: Optional[float] = None          # major units of `currency`
    currency: Optional[str] = None
    reference: Optional[str] = None         # Stripe refund id (re_...) or wallet transaction note
    payment_intent_id: Optional[str] = None
    tax_transaction_id: Optional[str] = None
    tax_reversal_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    refunded_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")
