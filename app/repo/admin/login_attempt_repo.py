"""Shared counter store of the admin login rate limit (TEOS-109).

Both calls are RPCs from supabase_ddl.sql (section "admin login rate limit"), so every
uvicorn worker and every instance counts the same attempts:

- ``begin``: counts the attempts of the last ``window_seconds`` for the IP and for the email,
  atomically; refuses when either reached ``limit``, else records this attempt and returns its id;
- ``finish``: ``success`` deletes the attempts of that IP + email pair, ``aborted`` only this one.
"""
from typing import Any, Optional, Tuple

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository

OUTCOME_SUCCESS = "success"
OUTCOME_ABORTED = "aborted"


def _unwrap(result: Any) -> dict:
    """A scalar jsonb RPC result can arrive wrapped in a list."""
    if isinstance(result, list):
        result = result[0] if result else {}
    return result if isinstance(result, dict) else {}


class LoginAttemptRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_ADMIN_LOGIN_ATTEMPT)

    def begin(self, ip: str, email: str, limit: int, window_seconds: int) -> Tuple[bool, Optional[int]]:
        """``(allowed, attempt_id)``; ``attempt_id`` is None when the attempt was refused."""
        body = _unwrap(self.rpc("admin_login_attempt_begin", {
            "p_ip": ip, "p_email": email, "p_limit": limit, "p_window_seconds": window_seconds,
        }))
        if "allowed" not in body:
            raise ValueError("admin_login_attempt_begin returned no decision")
        return bool(body["allowed"]), body.get("attempt_id")

    def finish(self, attempt_id: int, outcome: str) -> None:
        self.rpc("admin_login_attempt_finish", {"p_attempt_id": attempt_id, "p_outcome": outcome})
