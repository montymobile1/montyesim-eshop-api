"""Admin trusted devices storage (TEOS-255): ``admin_devices``, ``admin_device_verification`` and ``admin_otp``.

SQL: supabase_ddl.sql section "admin trusted devices" (also migrations/admin/256-ddl.sql). Service role only.
Times written here are UTC ISO strings; "now" comparisons in filters use the API clock.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _unwrap(result: Any) -> dict:
    """A scalar jsonb RPC result can arrive wrapped in a list."""
    if isinstance(result, list):
        result = result[0] if result else {}
    return result if isinstance(result, dict) else {}


class AdminDeviceRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_ADMIN_DEVICES)

    # ---------- reads ----------

    def active_by_hash(self, admin_id: str, device_hash: str) -> Optional[Dict[str, Any]]:
        """The admin's active (not revoked) row for this browser, trusted or not."""
        response = self._run(lambda: self.table.select("*").eq("admin_id", admin_id).eq("device_hash", device_hash)
                             .is_("revoked_at", "null").limit(1).execute())
        return response.data[0] if response.data else None

    def by_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """The row holding this Supabase session: the active one if any, else the most recently revoked
        one (so the caller can tell "device revoked" from "no device")."""
        response = self._run(lambda: self.table.select("*").eq("session_id", session_id)
                             .order("revoked_at", desc=True, nullsfirst=True).limit(1).execute())
        return response.data[0] if response.data else None

    def list_active(self, admin_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        response = self._run(lambda: self.table.select("*").eq("admin_id", admin_id).is_("revoked_at", "null")
                             .order("last_used_at", desc=True).limit(limit).execute())
        return response.data or []

    def get_active(self, admin_id: str, device_id: str) -> Optional[Dict[str, Any]]:
        response = self._run(lambda: self.table.select("*").eq("id", device_id).eq("admin_id", admin_id)
                             .is_("revoked_at", "null").limit(1).execute())
        return response.data[0] if response.data else None

    def trusted_active(self, admin_id: str) -> List[Dict[str, Any]]:
        """Active rows still trusted now, least recently used first (the 5-device limit drops the first)."""
        now = iso(utc_now())
        response = self._run(lambda: self.table.select("*").eq("admin_id", admin_id).is_("revoked_at", "null")
                             .gt("trusted_until", now).order("last_used_at", desc=False).execute())
        return response.data or []

    # ---------- writes ----------

    def insert(self, row: Dict[str, Any]) -> Dict[str, Any]:
        response = self._run(lambda: self.table.insert(row).execute())
        return response.data[0]

    def update(self, device_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        response = self._run(lambda: self.table.update(patch).eq("id", device_id).execute())
        return response.data[0] if response.data else None

    def mark_revoked(self, device_ids: Iterable[str], revoked_by: Optional[str], reason: str) -> List[Dict[str, Any]]:
        """Marks the rows revoked (only those not revoked yet) and returns them, ``session_id`` included so
        the caller can end each session. Ending the sessions is the caller's job (``end_session``)."""
        ids = [str(i) for i in device_ids]
        if not ids:
            return []
        patch = {"revoked_at": iso(utc_now()), "revoked_by": revoked_by, "revoke_reason": reason}
        response = self._run(lambda: self.table.update(patch).in_("id", ids).is_("revoked_at", "null").execute())
        return response.data or []

    def record_token_iat(self, device_id: str, iat: int) -> Optional[Dict[str, Any]]:
        """Stores the ``iat`` of an access token the API handed out, keeping the max: only updates when
        ``last_token_iat`` is null or lower (two writes out of order never lower it). Returns the row when it
        changed, else None."""
        iat = int(iat)
        response = self._run(lambda: self.table.update({"last_token_iat": iat}).eq("id", device_id)
                             .or_(f"last_token_iat.is.null,last_token_iat.lt.{iat}").execute())
        return response.data[0] if response.data else None

    def end_session(self, session_id: str) -> Dict[str, Any]:
        """Deletes one Supabase session and its refresh tokens (SQL ``admin_end_session``)."""
        return _unwrap(self.rpc("admin_end_session", {"p_session_id": session_id}))


class AdminDeviceVerificationRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_ADMIN_DEVICE_VERIFICATION)

    def create(self, row: Dict[str, Any]) -> Dict[str, Any]:
        response = self._run(lambda: self.table.insert(row).execute())
        return response.data[0]

    def get(self, verification_id: str) -> Optional[Dict[str, Any]]:
        response = self._run(lambda: self.table.select("*").eq("id", verification_id).limit(1).execute())
        return response.data[0] if response.data else None

    def sends_since(self, admin_id: str, since: datetime) -> int:
        """Codes emailed to the admin since ``since``: one per verification plus its resends."""
        response = self._run(lambda: self.table.select("resend_count").eq("admin_id", admin_id)
                             .gte("created_at", iso(since)).execute())
        return sum(1 + int(row.get("resend_count") or 0) for row in (response.data or []))

    def claim(self, verification_id: str) -> Optional[Dict[str, Any]]:
        """Atomically takes the verification for one verify: only a live, unclaimed, unexpired row is returned.
        Two verifies at once: one gets the row, the other None."""
        now = iso(utc_now())
        response = self._run(lambda: self.table.update({"consumed_at": now}).eq("id", verification_id)
                             .is_("consumed_at", "null").is_("failed_at", "null").gt("expires_at", now).execute())
        return response.data[0] if response.data else None

    def failed_attempt(self, verification_id: str, max_attempts: int) -> Dict[str, Any]:
        """A claimed verify had a wrong code: releases the claim and counts the attempt (dead at the max).
        ``{"found", "attempts", "attempts_left", "dead"}``."""
        return _unwrap(self.rpc("admin_device_verification_failed_attempt",
                                {"p_verification_id": verification_id, "p_max": max_attempts}))

    def release(self, verification_id: str) -> None:
        """Undoes a claim without counting an attempt (Supabase Auth was down during the verify)."""
        self._run(lambda: self.table.update({"consumed_at": None}).eq("id", verification_id)
                  .is_("failed_at", "null").execute())

    def fail_open(self, admin_id: str) -> int:
        """Kills the admin's open verifications (not consumed, not failed, not expired): only the newest verification
        may take guesses. Returns how many."""
        now = iso(utc_now())
        response = self._run(lambda: self.table.update({"failed_at": now}).eq("admin_id", admin_id)
                             .is_("consumed_at", "null").is_("failed_at", "null").gt("expires_at", now).execute())
        return len(response.data or [])

    def mark_failed(self, verification_id: str) -> None:
        self._run(lambda: self.table.update({"failed_at": iso(utc_now())}).eq("id", verification_id).execute())

    def record_resend(self, verification_id: str, resend_count: int, expires_at: datetime) -> Optional[Dict[str, Any]]:
        """Counts a resend only if nobody else did meanwhile (``resend_count`` unchanged)."""
        patch = {"resend_count": resend_count + 1, "last_sent_at": iso(utc_now()), "expires_at": iso(expires_at)}
        response = self._run(lambda: self.table.update(patch).eq("id", verification_id)
                             .eq("resend_count", resend_count).is_("consumed_at", "null")
                             .is_("failed_at", "null").execute())
        return response.data[0] if response.data else None


class AdminOtpRepo(AdminBaseRepository):
    """``admin_otp``: the sign-in codes, one or more per ``admin_device_verification`` row (``verification_id``),
    like ``user_otp`` for the customers. Only the
    HMAC-SHA256 of a code (``OTP_HMAC_SECRET``, ``user_otp_service.hash_otp``) is stored, never the code."""

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_ADMIN_OTP)

    def create(self, row: Dict[str, Any]) -> Dict[str, Any]:
        response = self._run(lambda: self.table.insert(row).execute())
        return response.data[0]

    def active(self, verification_id: str) -> Optional[Dict[str, Any]]:
        """The verification's live code: the newest row not used and not expired, else None."""
        now = iso(utc_now())
        response = self._run(lambda: self.table.select("*").eq("verification_id", verification_id).eq("is_used", False)
                             .gt("expire_at", now).order("id", desc=True).limit(1).execute())
        return response.data[0] if response.data else None

    def expire_active(self, verification_id: str) -> int:
        """A resend: the verification's live codes stop working (``expire_at`` = now). Returns how many."""
        now = iso(utc_now())
        response = self._run(lambda: self.table.update({"expire_at": now}).eq("verification_id", verification_id)
                             .eq("is_used", False).gt("expire_at", now).execute())
        return len(response.data or [])

    def mark_used(self, otp_id: Any) -> None:
        self._run(lambda: self.table.update({"is_used": True}).eq("id", otp_id).execute())
