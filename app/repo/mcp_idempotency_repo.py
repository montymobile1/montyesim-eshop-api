from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.config.db import DatabaseTables
from app.config.mcp_constants import DEFAULT_IDEMPOTENCY_TTL_SECONDS, McpClaimOutcome
from app.exceptions import DatabaseException
from app.models.mcp import McpIdempotencyClaim, McpPurchaseIdempotencyModel
from app.repo.base_repo import BaseRepository

#: PostgreSQL function performing the atomic claim. Shipped by the SQL migration
#: ``migrations/20260731_0001_mcp_purchase_idempotency.sql``.
CLAIM_FUNCTION_NAME = "mcp_claim_purchase_idempotency"


class McpPurchaseIdempotencyRepo(BaseRepository):
    """Durable, database backed idempotency records for MCP purchases.

    Concurrency is resolved by PostgreSQL (unique constraint +
    ``INSERT ... ON CONFLICT DO NOTHING`` inside a SQL function), never by a
    process local lock and never by a check-then-insert sequence.
    """

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_MCP_PURCHASE_IDEMPOTENCY, McpPurchaseIdempotencyModel)

    def claim(self, user_id: str, operation: str, idempotency_key_hash: str, request_hash: str,
              ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS) -> McpIdempotencyClaim:
        """Atomically claim ``(user_id, operation, key_hash)`` or report why we cannot."""
        try:
            response = self.client.rpc(CLAIM_FUNCTION_NAME, params={
                "p_user_id": user_id,
                "p_operation": operation,
                "p_idempotency_key_hash": idempotency_key_hash,
                "p_request_hash": request_hash,
                "p_ttl_seconds": ttl_seconds,
            }).execute()
        except Exception as e:
            raise DatabaseException(str(e))

        data = response.data
        if isinstance(data, list):
            data = data[0] if data else None
        if not data:
            # The function always returns exactly one row; an empty result means the
            # claim could not be established, so we must not execute anything.
            return McpIdempotencyClaim(outcome=McpClaimOutcome.RETRY)
        return McpIdempotencyClaim(**data)

    def get_for_user(self, record_id: str, user_id: str) -> Optional[McpPurchaseIdempotencyModel]:
        """Fetch a record scoped to its owner. Cross-user reads are impossible by construction."""
        return self.get_first_by(where={"id": record_id, "user_id": user_id})

    def attach_order(self, record_id: str, user_id: str, order_id: str):
        """Persist the created order id as early as possible for crash reconciliation.

        This also marks the record as having side effects, which permanently removes it
        from the set of records that may ever be re-executed under the same key.
        """
        return self.update_by(where={"id": record_id, "user_id": user_id},
                              data={"order_id": order_id, "has_side_effects": True,
                                    "updated_at": self.__now()})

    def mark_side_effect(self, record_id: str, user_id: str):
        """Record that something irreversible happened (wallet debit, provisioning call).

        Once set, the claim function can never hand this key back out for execution,
        regardless of the terminal status it ends up in.
        """
        return self.update_by(where={"id": record_id, "user_id": user_id},
                              data={"has_side_effects": True, "updated_at": self.__now()})

    def mark_terminal(self, record_id: str, user_id: str, status: str, response_code: int,
                      response_body: Optional[Dict[str, Any]] = None, error_code: Optional[str] = None,
                      order_id: Optional[str] = None):
        """Persist the terminal (or ambiguous) outcome of an execution."""
        data: Dict[str, Any] = {
            "status": status,
            "response_code": response_code,
            "response_body": response_body,
            "error_code": error_code,
            "updated_at": self.__now(),
        }
        if order_id is not None:
            data["order_id"] = order_id
        return self.update_by(where={"id": record_id, "user_id": user_id}, data=data)

    @staticmethod
    def __now() -> str:
        return datetime.now(tz=timezone.utc).isoformat()
