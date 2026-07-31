"""An in-process stand-in for the Supabase client.

It exists so the MCP tests can exercise the *real* repositories, services and FastAPI
routes without ever touching Supabase, Stripe, the eSIM Hub, QA or production.

What it deliberately reproduces from PostgreSQL:
  * unique constraints, raised as an error on INSERT, and
  * the ``mcp_claim_purchase_idempotency`` function, executed under a lock so that
    concurrent callers observe the same serialization a real transaction gives them.
"""

import itertools
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

#: Tables whose primary key is a bigint identity rather than a uuid.
INT_ID_TABLES = {"user_profile_bundle", "device", "notification", "app_config", "audit_log"}


class UniqueViolation(Exception):
    """Mirrors the postgrest error raised when a unique constraint is violated."""


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


class FakeDatabase:
    """Tiny relational store: ``{table_name: [row, ...]}`` plus unique constraints."""

    def __init__(self):
        self.tables: Dict[str, List[dict]] = {}
        self.unique_constraints: Dict[str, List[tuple]] = {
            "mcp_purchase_idempotency": [("user_id", "operation", "idempotency_key_hash")],
        }
        self.auth_users: Dict[str, dict] = {}
        self.lock = threading.RLock()
        self.counters: Dict[str, itertools.count] = {}
        #: Set by tests to make the claim RPC unavailable (migration not applied).
        self.rpc_handlers = {"mcp_claim_purchase_idempotency": self.claim_idempotency}

    # ---------------------------------------------------------------- helpers

    def rows(self, table: str) -> List[dict]:
        return self.tables.setdefault(table, [])

    def seed(self, table: str, row: dict) -> dict:
        with self.lock:
            self.rows(table).append(dict(row))
            return dict(row)

    def next_id(self, table: str):
        if table in INT_ID_TABLES:
            counter = self.counters.setdefault(table, itertools.count(1))
            return next(counter)
        return str(uuid.uuid4())

    def insert(self, table: str, data: dict) -> dict:
        with self.lock:
            row = dict(data)
            row.setdefault("id", self.next_id(table))
            row.setdefault("created_at", _iso(_now()))
            for constraint in self.unique_constraints.get(table, []):
                for existing in self.rows(table):
                    if all(existing.get(column) == row.get(column) for column in constraint):
                        raise UniqueViolation(
                            f"duplicate key value violates unique constraint on {table}{constraint}")
            self.rows(table).append(row)
            return dict(row)

    # ------------------------------------------------------------------- RPCs

    def claim_idempotency(self, params: dict) -> List[dict]:
        """Python transcription of ``mcp_claim_purchase_idempotency``.

        The lock stands in for the row lock a real transaction would take, so two
        concurrent callers can never both be told ``CLAIMED``.
        """
        table = "mcp_purchase_idempotency"
        user_id = params["p_user_id"]
        operation = params["p_operation"]
        key_hash = params["p_idempotency_key_hash"]
        request_hash = params["p_request_hash"]
        ttl_seconds = params.get("p_ttl_seconds", 86400)

        with self.lock:
            existing = next((row for row in self.rows(table)
                             if row.get("user_id") == user_id and row.get("operation") == operation
                             and row.get("idempotency_key_hash") == key_hash), None)
            if existing is None:
                row = self.insert(table, {
                    "user_id": user_id, "operation": operation, "idempotency_key_hash": key_hash,
                    "request_hash": request_hash, "status": "PROCESSING", "order_id": None,
                    "response_code": None, "response_body": None, "error_code": None,
                    "updated_at": _iso(_now()),
                    "expires_at": _iso(_now() + timedelta(seconds=ttl_seconds)),
                })
                return [self.__claim_row("CLAIMED", row)]

            expires_at = existing.get("expires_at")
            expired = bool(expires_at) and datetime.fromisoformat(expires_at) <= _now()
            if existing["status"] != "PROCESSING" and expired:
                existing.update({"request_hash": request_hash, "status": "PROCESSING", "order_id": None,
                                 "response_code": None, "response_body": None, "error_code": None,
                                 "updated_at": _iso(_now()),
                                 "expires_at": _iso(_now() + timedelta(seconds=ttl_seconds))})
                return [self.__claim_row("CLAIMED", existing)]
            if existing["request_hash"] != request_hash:
                return [self.__claim_row("CONFLICT", existing)]
            if existing["status"] == "FAILED_RETRYABLE":
                existing.update({"status": "PROCESSING", "response_code": None, "response_body": None,
                                 "error_code": None, "updated_at": _iso(_now())})
                return [self.__claim_row("CLAIMED", existing)]
            return [self.__claim_row(existing["status"], existing)]

    @staticmethod
    def __claim_row(outcome: str, row: dict) -> dict:
        return {
            "outcome": outcome,
            "record_id": row.get("id"),
            "status": row.get("status"),
            "order_id": row.get("order_id"),
            "response_code": row.get("response_code"),
            "response_body": row.get("response_body"),
            "error_code": row.get("error_code"),
            "request_hash": row.get("request_hash"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "expires_at": row.get("expires_at"),
        }


class _Query:
    """Minimal postgrest-style query builder."""

    def __init__(self, db: FakeDatabase, table: str, action: str, payload: Any = None,
                 on_conflict: Optional[str] = None):
        self.db = db
        self.table = table
        self.action = action
        self.payload = payload
        self.on_conflict = on_conflict
        self.filters: List[tuple] = []
        self._limit: Optional[int] = None
        self._offset: int = 0
        self._order: Optional[tuple] = None

    # -- filters ---------------------------------------------------------

    def eq(self, column: str, value):
        self.filters.append(("eq", column, value))
        return self

    def neq(self, column: str, value):
        self.filters.append(("neq", column, value))
        return self

    def gte(self, column: str, value):
        self.filters.append(("gte", column, value))
        return self

    def lt(self, column: str, value):
        self.filters.append(("lt", column, value))
        return self

    def filter(self, column: str, operator: str, value):
        self.filters.append((operator, column, value))
        return self

    def limit(self, value: int):
        self._limit = value
        return self

    def offset(self, value: int):
        self._offset = value
        return self

    def order(self, column: str, desc: bool = False):
        self._order = (column, desc)
        return self

    # -- execution -------------------------------------------------------

    def __matches(self, row: dict) -> bool:
        for operator, column, value in self.filters:
            # JSON-path style filters ("bundle_data ->> bundle_code") are not needed by
            # the MCP tests; treat them as non-matching rather than guessing.
            actual = row.get(column.strip()) if "->" not in column else None
            if operator in ("eq", "in") and str(actual) != str(value):
                return False
            if operator == "neq" and str(actual) == str(value):
                return False
            if operator == "gte" and not (actual is not None and str(actual) >= str(value)):
                return False
            if operator == "lt" and not (actual is not None and str(actual) < str(value)):
                return False
        return True

    def execute(self):
        with self.db.lock:
            rows = self.db.rows(self.table)
            if self.action == "insert":
                payload = self.payload if isinstance(self.payload, list) else [self.payload]
                inserted = [self.db.insert(self.table, item) for item in payload]
                return SimpleNamespace(data=inserted)
            if self.action == "upsert":
                payload = self.payload if isinstance(self.payload, list) else [self.payload]
                return SimpleNamespace(data=[self.db.insert(self.table, item) for item in payload])
            selected = [row for row in rows if self.__matches(row)]
            if self.action == "select":
                if self._order:
                    column, desc = self._order
                    selected = sorted(selected, key=lambda r: (r.get(column) is None, r.get(column)),
                                      reverse=desc)
                selected = selected[self._offset:]
                if self._limit is not None:
                    selected = selected[:self._limit]
                return SimpleNamespace(data=[dict(row) for row in selected])
            if self.action == "update":
                for row in selected:
                    row.update(self.payload)
                return SimpleNamespace(data=[dict(row) for row in selected])
            if self.action == "delete":
                for row in selected:
                    rows.remove(row)
                return SimpleNamespace(data=[dict(row) for row in selected])
            raise AssertionError(f"unsupported action {self.action}")


class _Table:
    def __init__(self, db: FakeDatabase, name: str):
        self.db = db
        self.name = name

    def select(self, *_args, **_kwargs):
        return _Query(self.db, self.name, "select")

    def insert(self, data):
        return _Query(self.db, self.name, "insert", payload=data)

    def upsert(self, data, on_conflict: str = None):
        return _Query(self.db, self.name, "upsert", payload=data, on_conflict=on_conflict)

    def update(self, data):
        return _Query(self.db, self.name, "update", payload=data)

    def delete(self):
        return _Query(self.db, self.name, "delete")


class _Rpc:
    def __init__(self, db: FakeDatabase, name: str, params: dict):
        self.db = db
        self.name = name
        self.params = params

    def execute(self):
        handler = self.db.rpc_handlers.get(self.name)
        if handler is None:
            raise AssertionError(f"function {self.name} does not exist")
        return SimpleNamespace(data=handler(self.params))


class _Auth:
    def __init__(self, db: FakeDatabase):
        self.db = db

    def get_user(self, jwt: str = None):
        user = self.db.auth_users.get(jwt)
        if user is None:
            raise AssertionError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(
            id=user["id"], email=user.get("email"), user_metadata=user.get("user_metadata", {}),
            is_anonymous=user.get("is_anonymous", False)))


class FakeSupabaseClient:
    def __init__(self, db: FakeDatabase):
        self.db = db
        self.auth = _Auth(db)

    def table(self, name: str) -> _Table:
        return _Table(self.db, name)

    def rpc(self, name: str, params: dict = None):
        return _Rpc(self.db, name, params or {})


#: One shared database for the whole test session; ``reset()`` clears it between tests.
DATABASE = FakeDatabase()


def fake_create_client(*_args, **_kwargs) -> FakeSupabaseClient:
    """Drop-in replacement for ``supabase.create_client``."""
    return FakeSupabaseClient(DATABASE)


def reset() -> FakeDatabase:
    with DATABASE.lock:
        DATABASE.tables.clear()
        DATABASE.auth_users.clear()
        DATABASE.counters.clear()
    return DATABASE
