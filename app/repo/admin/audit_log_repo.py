"""audit_log access for the admin API. Rows are written by SQL functions only, never inserted directly."""
from typing import Any, Dict, Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, AuditContext


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
