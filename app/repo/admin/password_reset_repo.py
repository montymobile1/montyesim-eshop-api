"""``admin_password_reset`` (TEOS-337/339): the reset links sent from the Admins page.

One row per link. Only the SHA-256 of the link's token is stored (``token_hash``), never the token. A link works
while ``used_at`` and ``cancelled_at`` are null and ``expires_at`` is in the future. Using it is one conditional
update (``claim``), so two submissions at once cannot both use it.
"""
from datetime import datetime
from typing import Any, Dict, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository
from app.repo.admin.trusted_device_repo import iso


class AdminPasswordResetRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_ADMIN_PASSWORD_RESET)

    def create(self, row: Dict[str, Any]) -> Dict[str, Any]:
        response = self._run(lambda: self.table.insert(row).execute())
        return response.data[0]

    def sent_since(self, admin_id: str, since: datetime) -> int:
        """Links created for the admin since ``since`` (used, cancelled or not): the sending limit."""
        response = self._run(lambda: self.table.select("id").eq("admin_id", admin_id)
                             .gte("created_at", iso(since)).execute())
        return len(response.data or [])

    def cancel_pending(self, admin_id: str, now: datetime, except_id: Optional[Any] = None) -> int:
        """Every link of the admin that could still be used stops working (``cancelled_at`` = now), except
        ``except_id`` (the link just sent). Returns how many."""
        def run():
            query = (self.table.update({"cancelled_at": iso(now)}).eq("admin_id", admin_id)
                     .is_("used_at", "null").is_("cancelled_at", "null").gt("expires_at", iso(now)))
            if except_id is not None:
                query = query.neq("id", str(except_id))
            return query.execute()
        return len(self._run(run).data or [])

    def cancel(self, link_id: Any, now: datetime) -> None:
        """This one link stops working (its email could not be sent)."""
        self._run(lambda: self.table.update({"cancelled_at": iso(now)}).eq("id", str(link_id)).execute())

    def claim(self, token_hash: str, now: datetime) -> Optional[Dict[str, Any]]:
        """Atomically uses the link: only an unused, uncancelled, unexpired row is returned (``used_at`` = now),
        else None."""
        response = self._run(lambda: self.table.update({"used_at": iso(now)}).eq("token_hash", token_hash)
                             .is_("used_at", "null").is_("cancelled_at", "null").gt("expires_at", iso(now))
                             .execute())
        return response.data[0] if response.data else None

    def release(self, link_id: Any) -> None:
        """Gives a claimed link back (``used_at`` = null) when the password could not be set for a reason the admin
        can fix (e.g. a password Supabase refuses), so the same link can be tried again. A link cancelled
        meanwhile stays cancelled."""
        self._run(lambda: self.table.update({"used_at": None}).eq("id", link_id).is_("cancelled_at", "null")
                  .execute())
