"""audit_log access for the admin API. Rows are written by SQL functions only, never inserted directly."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, AuditContext
from app.schemas.admin.common import PageParams


@dataclass(frozen=True)
class AuditSearch:
    """One page of audit rows (raw, as stored), the total and the per-table counts."""
    rows: List[Dict[str, Any]]
    total: int
    facets_by_table: Dict[str, int] = field(default_factory=dict)


class AuditLogRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_AUDIT_LOG)

    def write(self, ctx: AuditContext, *, table: str, operation: str, record_id: Optional[Any] = None,
              old_data: Optional[Dict[str, Any]] = None, new_data: Optional[Dict[str, Any]] = None,
              details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One audit row through ``admin_write_audit``. The SQL function adds ip_address, reason and
        client_reported=false to operation_details."""
        return self.rpc("admin_write_audit", {
            "p_table": table,
            "p_operation": operation,
            "p_record_id": None if record_id is None else str(record_id),
            "p_old_data": old_data,
            "p_new_data": new_data,
            "p_actor": ctx.actor_id,
            "p_ip": ctx.ip,
            "p_reason": ctx.reason,
            "p_details": details or {},
        })

    # ---------- reads (MS-5046 settings history, MS-5048 audit log) ----------

    def search(self, page: PageParams, *, table: Optional[str] = None, operation: Optional[str] = None,
               changed_by: Optional[str] = None, changed_from: Optional[datetime] = None,
               changed_to: Optional[datetime] = None, setting_key: Optional[str] = None) -> AuditSearch:
        """One page through ``admin_audit_log_search`` (supabase_ddl.sql, section 110_audit_search.sql), ordered
        ``changed_at desc, id desc``. ``operation`` is matched against the normalised operation (legacy
        aliases mapped in SQL); ``changed_to`` is exclusive; ``setting_key`` keeps app_config rows where
        that key's value changed. ``facets_by_table`` counts every filter except ``table``."""
        result = self.rpc("admin_audit_log_search", {
            "p_table": table,
            "p_operation": operation,
            "p_changed_by": changed_by,
            "p_from": changed_from.isoformat() if changed_from else None,
            "p_to": changed_to.isoformat() if changed_to else None,
            "p_setting_key": setting_key,
            "p_limit": page.limit,
            "p_offset": page.offset,
        }) or {}
        if isinstance(result, list):  # a scalar jsonb RPC result can arrive wrapped in a list
            result = result[0] if result else {}
        return AuditSearch(rows=list(result.get("data") or []), total=int(result.get("total") or 0),
                           facets_by_table=dict(result.get("facets_by_table") or {}))

    def get(self, audit_id: int) -> Optional[Dict[str, Any]]:
        """One audit row by id, or None."""
        return self.one(audit_id)
