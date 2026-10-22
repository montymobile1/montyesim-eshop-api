"""app_config for the admin Settings pages (MS-5091).

Reads go through PostgREST; the save goes through ``admin_upsert_settings``
(migrations/admin/120_settings.sql), which upserts, deletes, writes ONE audit row and rotates
APP_CACHE_KEY when FORBIDDEN_COUNTRIES changed, all in one transaction.
"""
from typing import Any, Dict, Iterable, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, AuditContext, Paged
from app.schemas.admin.common import PageParams

COLUMNS = "id,key,value,created_at"
#: GET /settings also returns the description (app_config.description, TEOS-134, supabase_ddl.sql).
LIST_COLUMNS = "id,key,value,description,created_at"


class AppConfigAdminRepo(AdminBaseRepository):
    UNIQUE_CONFLICTS = {"app_config_key_key": "duplicate_key"}

    def __init__(self):
        super().__init__(DatabaseTables.TABLE_APP_CONFIG)

    def search(self, page: PageParams, *, key: Optional[str] = None, search: Optional[str] = None) -> Paged:
        """One page of settings, newest first; ``key`` is an exact match, ``search`` a part of the key
        (case-insensitive, matched literally)."""
        query = self.filters(self.q(LIST_COLUMNS), eq={"key": key}, ilike={"key": search})
        return self.paged(query, page)

    def value_of(self, key: str) -> Optional[str]:
        """The value of one setting, None when the row is missing."""
        return (self.by_keys([key]).get(key) or {}).get("value")

    def by_keys(self, keys: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """``{key: row}`` for the given keys; missing keys are absent."""
        unique = sorted({k for k in keys if k})
        if not unique:
            return {}
        query = self.filters(self.table.select(COLUMNS), in_={"key": unique})
        rows = self._run(lambda: query.execute()).data or []
        return {row["key"]: row for row in rows}

    def upsert_settings(self, items: List[Dict[str, str]], deleted_keys: List[str],
                        ctx: AuditContext) -> Dict[str, Any]:
        """``{updated[], deleted[], cache_key_rotated}``. AD409 no_change -> 409, AD422 -> 422."""
        return self.rpc("admin_upsert_settings", {
            "p_items": items,
            "p_deleted": deleted_keys,
            "p_actor": ctx.actor_id,
            "p_ip": ctx.ip,
            "p_reason": ctx.reason,
        })
