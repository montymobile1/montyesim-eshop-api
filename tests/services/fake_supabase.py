"""Minimal in-memory stand-in for the Supabase client used by the repositories.

It lets the integration tests exercise the real repository/service code (filters,
aggregation, limit rules) while only the database driver is faked. The
`reserve_wallet_top_up_daily_limit` and `complete_wallet_top_up_reservation` RPCs mirror
the plpgsql functions shipped in `supabase_ddl.sql`: they serialize concurrent calls (the
`FOR UPDATE` wallet row lock), expire abandoned reservations, validate the daily limits
before reserving capacity, and settle a paid top-up exactly once.
"""
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

DATETIME_COLUMNS = ("created_at", "updated_at", "expires_at")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _naive_utc_iso(value: str) -> str:
    """Store a timestamp the way Postgres stores it in a `timestamp` column: naive UTC."""
    return _as_comparable("created_at", value).isoformat()


def _as_comparable(column: str, value: Any):
    if column in DATETIME_COLUMNS and isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    return value


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, table: "FakeTable", operation: str, payload: Optional[Dict[str, Any]] = None):
        self.table = table
        self.operation = operation
        self.payload = payload
        self.filters: List[tuple] = []
        self._limit: Optional[int] = None
        self._offset: int = 0
        self._order: Optional[tuple] = None

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self.filters.append(("eq", column, value))
        return self

    def gte(self, column: str, value: Any) -> "FakeQuery":
        self.filters.append(("gte", column, value))
        return self

    def lt(self, column: str, value: Any) -> "FakeQuery":
        self.filters.append(("lt", column, value))
        return self

    def gt(self, column: str, value: Any) -> "FakeQuery":
        self.filters.append(("gt", column, value))
        return self

    def filter(self, column: str, operator: str, value: Any) -> "FakeQuery":
        self.filters.append((operator, column, value))
        return self

    def limit(self, value: int) -> "FakeQuery":
        self._limit = value
        return self

    def offset(self, value: int) -> "FakeQuery":
        self._offset = value
        return self

    def order(self, column: str, desc: bool = False) -> "FakeQuery":
        self._order = (column, desc)
        return self

    def __matches(self, row: Dict[str, Any]) -> bool:
        for operator, column, value in self.filters:
            row_value = _as_comparable(column, row.get(column))
            if operator == "in":
                if str(row_value) not in value.strip("()").split(","):
                    return False
                continue
            expected = _as_comparable(column, value)
            if operator == "eq" and row_value != expected:
                return False
            if operator == "gte" and not (row_value is not None and row_value >= expected):
                return False
            if operator == "gt" and not (row_value is not None and row_value > expected):
                return False
            if operator == "lt" and not (row_value is not None and row_value < expected):
                return False
        return True

    def execute(self) -> FakeResponse:
        with self.table.db.lock:
            rows = [row for row in self.table.rows if self.__matches(row)]
            if self.operation == "select":
                if self._order:
                    column, desc = self._order
                    rows = sorted(rows, key=lambda item: item.get(column) or "", reverse=desc)
                rows = rows[self._offset:]
                if self._limit is not None:
                    rows = rows[:self._limit]
                return FakeResponse([dict(row) for row in rows])
            if self.operation == "insert":
                return FakeResponse([dict(self.table.insert_row(self.payload))])
            if self.operation == "update":
                for row in rows:
                    row.update(self.payload)
                return FakeResponse([dict(row) for row in rows])
            if self.operation == "delete":
                for row in rows:
                    self.table.rows.remove(row)
                return FakeResponse([dict(row) for row in rows])
            raise NotImplementedError(self.operation)


class FakeTable:
    def __init__(self, db: "FakeSupabaseClient", name: str):
        self.db = db
        self.name = name
        self.rows: List[Dict[str, Any]] = db.tables.setdefault(name, [])

    def insert_row(self, data: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(data)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", utc_now_iso())
        self.rows.append(row)
        return row

    def select(self, columns: str = "*") -> FakeQuery:
        return FakeQuery(self, "select")

    def insert(self, data: Dict[str, Any]) -> FakeQuery:
        return FakeQuery(self, "insert", data)

    def update(self, data: Dict[str, Any]) -> FakeQuery:
        return FakeQuery(self, "update", data)

    def delete(self) -> FakeQuery:
        return FakeQuery(self, "delete")

    def upsert(self, data: Dict[str, Any], on_conflict: str = None) -> FakeQuery:
        return FakeQuery(self, "insert", data)


class FakeRpc:
    def __init__(self, client: "FakeSupabaseClient", function_name: str, params: Dict[str, Any]):
        self.client = client
        self.function_name = function_name
        self.params = params

    def execute(self) -> FakeResponse:
        handler = getattr(self.client, f"rpc_{self.function_name}", None)
        if handler is None:
            raise NotImplementedError(self.function_name)
        return FakeResponse(handler(self.params))


class FakeSupabaseClient:
    """Shared in-memory database; every repository instance sees the same rows."""

    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.lock = threading.RLock()
        # delay injected inside the critical section to make concurrency tests deterministic
        self.rpc_delay_seconds = 0.0
        self.rpc_calls: List[Dict[str, Any]] = []

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)

    def rpc(self, function_name: str, params: Dict[str, Any]) -> FakeRpc:
        return FakeRpc(self, function_name, params)

    def seed(self, table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
        return FakeTable(self, table_name).insert_row(row)

    def rows(self, table_name: str) -> List[Dict[str, Any]]:
        return self.tables.setdefault(table_name, [])

    def __wallet_of(self, user_id: str) -> Optional[Dict[str, Any]]:
        return next((row for row in self.rows("user_wallet") if row.get("user_id") == user_id), None)

    def __daily_usage(self, wallet: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Completed top-ups plus pending reservations of the window, like the SQL functions."""
        window_start = _as_comparable("created_at", params["p_window_start"])
        window_end = _as_comparable("created_at", params["p_window_end"])

        def in_window(row) -> bool:
            return window_start <= _as_comparable("created_at", row["created_at"]) < window_end

        completed = [row for row in self.rows("user_wallet_transaction")
                     if row.get("wallet_id") == wallet["id"]
                     and row.get("source") == params["p_source"]
                     and row.get("status") == params["p_success_status"]
                     and in_window(row)]
        pending = [row for row in self.rows("user_wallet_top_up_reservation")
                   if row.get("wallet_id") == wallet["id"]
                   and row.get("status") == "pending"
                   and in_window(row)]
        return {
            "successful_topup_count": len(completed),
            "successful_topup_amount": sum((Decimal(str(row["amount"])) for row in completed), Decimal("0")),
            "pending_reservation_count": len(pending),
            "pending_reservation_amount": sum((Decimal(str(row["amount"])) for row in pending), Decimal("0")),
        }

    def rpc_reserve_wallet_top_up_daily_limit(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.rpc_calls.append(params)
        # the whole body runs under the lock, like the plpgsql function running inside a
        # transaction that holds `select ... for update` on the wallet row
        with self.lock:
            wallet = self.__wallet_of(params["p_user_id"])
            if wallet is None:
                return {"status": "wallet_not_found"}

            now = utc_now()
            # requests abandoned before their payment intent existed stop holding capacity;
            # reservations that already carry a payment are only released once the provider
            # confirms the payment is dead (application side reconciliation)
            for row in self.rows("user_wallet_top_up_reservation"):
                if (row.get("wallet_id") == wallet["id"] and row.get("status") == "pending"
                        and not row.get("payment_reference")
                        and _as_comparable("expires_at", row["expires_at"]) <= now):
                    row["status"] = "expired"

            usage = self.__daily_usage(wallet, params)
            amount = Decimal(str(params["p_amount"]))
            reported = {key: float(value) if isinstance(value, Decimal) else value for key, value in usage.items()}

            if self.rpc_delay_seconds:
                time.sleep(self.rpc_delay_seconds)

            if usage["successful_topup_count"] + usage["pending_reservation_count"] >= params["p_max_count"]:
                return {"status": "count_limit_reached", **reported}
            if (usage["successful_topup_amount"] + usage["pending_reservation_amount"] + amount
                    > Decimal(str(params["p_max_amount"]))):
                return {"status": "amount_limit_exceeded", **reported}

            reservation = FakeTable(self, "user_wallet_top_up_reservation").insert_row({
                "user_id": params["p_user_id"],
                "wallet_id": wallet["id"],
                "order_id": params.get("p_order_id"),
                "amount": float(amount),
                "currency": params["p_currency"],
                "status": "pending",
                "payment_reference": None,
                "transaction_id": None,
                "expires_at": _naive_utc_iso(params["p_expires_at"]),
            })
            return {"status": "reserved", "reservation_id": reservation["id"], **reported}

    def rpc_complete_wallet_top_up_reservation(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.rpc_calls.append(params)
        with self.lock:
            wallet = self.__wallet_of(params["p_user_id"])
            if wallet is None:
                return {"status": "wallet_not_found"}

            reference = params.get("p_payment_reference")
            order_id = params.get("p_order_id")

            if reference is not None:
                # already credited: never credit or refund the same payment twice
                existing = next((row for row in self.rows("user_wallet_transaction")
                                 if row.get("wallet_id") == wallet["id"]
                                 and row.get("payment_reference") == reference), None)
                if existing is not None:
                    return {"status": "already_processed", "transaction_id": existing["id"],
                            "balance": wallet["amount"]}
                # already owed a refund: a replayed callback must not credit it either
                refund = next((row for row in self.rows("user_wallet_top_up_refund")
                               if row.get("payment_reference") == reference), None)
                if refund is not None:
                    return {"status": "refund_required", "refund_id": refund["id"],
                            "refund_status": refund["status"], "refund_reason": refund["reason"],
                            "provider_refund_reference": refund.get("provider_refund_reference"),
                            "refund_amount": refund["amount"], "refund_currency": refund["currency"],
                            "attempt_count": refund.get("attempt_count", 0),
                            "reservation_id": refund.get("reservation_id"), "balance": wallet["amount"]}

            matches = [row for row in self.rows("user_wallet_top_up_reservation")
                       if (reference is not None and row.get("payment_reference") == reference)
                       or (order_id is not None and row.get("order_id") == order_id)]
            reservation = sorted(matches, key=lambda row: row["created_at"])[-1] if matches else None
            reservation_status = reservation["status"] if reservation else None

            if reservation is not None and reservation["status"] == "completed":
                return {"status": "already_processed", "transaction_id": reservation.get("transaction_id"),
                        "reservation_id": reservation["id"], "reservation_status": reservation_status,
                        "balance": wallet["amount"]}

            if self.rpc_delay_seconds:
                time.sleep(self.rpc_delay_seconds)

            amount = Decimal(str(params["p_amount"]))
            # a pending reservation already holds this capacity, anything else is re-checked
            if reservation is None or reservation["status"] != "pending":
                usage = self.__daily_usage(wallet, params)
                reason = None
                if usage["successful_topup_count"] + usage["pending_reservation_count"] >= params["p_max_count"]:
                    reason = "count_limit_reached"
                elif (usage["successful_topup_amount"] + usage["pending_reservation_amount"] + amount
                      > Decimal(str(params["p_max_amount"]))):
                    reason = "amount_limit_exceeded"
                if reason is not None:
                    refund = FakeTable(self, "user_wallet_top_up_refund").insert_row({
                        "user_id": params["p_user_id"],
                        "wallet_id": wallet["id"],
                        "order_id": order_id,
                        "reservation_id": reservation["id"] if reservation else None,
                        "payment_reference": reference,
                        "provider_refund_reference": None,
                        "amount": float(amount),
                        "currency": wallet["currency"],
                        "status": "pending",
                        "reason": reason,
                        "attempt_count": 0,
                        "last_error": None,
                        "last_attempt_at": None,
                    })
                    return {"status": "refund_required", "refund_id": refund["id"], "refund_status": "pending",
                            "refund_reason": reason, "refund_amount": float(amount),
                            "refund_currency": wallet["currency"], "attempt_count": 0,
                            "reservation_id": refund["reservation_id"], "reservation_status": reservation_status,
                            "balance": wallet["amount"]}

            transaction = FakeTable(self, "user_wallet_transaction").insert_row({
                "wallet_id": wallet["id"],
                "amount": float(amount),
                "status": params["p_success_status"],
                "source": params["p_source"],
                "payment_reference": reference,
            })
            wallet["amount"] = float(Decimal(str(wallet["amount"])) + amount)
            if reservation is not None:
                reservation.update({"status": "completed", "transaction_id": transaction["id"],
                                    "payment_reference": reference or reservation.get("payment_reference"),
                                    "updated_at": utc_now_iso()})
            return {"status": "credited", "transaction_id": transaction["id"],
                    "reservation_id": reservation["id"] if reservation else None,
                    "reservation_status": reservation_status, "balance": wallet["amount"]}
