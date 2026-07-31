"""Optional observation hooks for the shared purchase flow.

``UserBundleService.assign`` accepts an optional ``execution_context``. When it is
``None`` - which is what every legacy caller passes, because it is the default -
the flow behaves exactly as before: no hook is invoked and no branch is taken.

The hooks are *observers only*: they must never raise, never mutate the order and
never influence control flow inside the shared purchase code. They exist so a
caller (today: the MCP wallet purchase endpoint) can correlate the order it just
created and can tell "nothing was charged" apart from "charged, provisioning
unclear" without duplicating the purchase logic.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PurchaseExecutionContext(Protocol):  # pragma: no cover - structural type only
    def on_order_created(self, order_id: str) -> None:
        """Called immediately after the backend order row is created."""

    def on_wallet_debited(self, amount: float, currency: str) -> None:
        """Called immediately after the wallet debit transaction is recorded."""

    def on_provisioning_result(self, result: Any) -> None:
        """Called with the value returned by the provisioning step."""
