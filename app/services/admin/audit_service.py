"""Audit rows for admin actions that are not a single database write.

Database writes are audited by the ``admin_*_audited`` SQL functions in the same transaction
(see ``AdminBaseRepository.insert_audited`` and friends). This service is for the other cases:
Supabase Auth changes such as ban/unban, exports, and multi-step actions.
"""
from typing import Any, Dict, Optional

from fastapi import Request

from app.dependencies.admin_security import AdminUser, client_ip
from app.repo.admin.audit_log_repo import AuditLogRepo
from app.repo.admin.base import AuditContext


def audit_context(user: AdminUser, request: Request, reason: Optional[str] = None) -> AuditContext:
    """The actor, IP and reason of the current request, for audited writes."""
    return AuditContext(actor_id=user.id, ip=client_ip(request), reason=reason)


class AuditService:
    def __init__(self, repo: Optional[AuditLogRepo] = None):
        self._repo = repo or AuditLogRepo()

    def log(self, ctx: AuditContext, table: str, operation: str, *, record_id: Optional[Any] = None,
            old: Optional[Dict[str, Any]] = None, new: Optional[Dict[str, Any]] = None,
            rows_affected: Optional[int] = None, **details: Any) -> Dict[str, Any]:
        """Writes one audit row. Raises on failure: callers decide whether to undo their action."""
        if rows_affected is not None:
            details["rows_affected"] = rows_affected
        return self._repo.write(ctx, table=table, operation=operation, record_id=record_id,
                                old_data=old, new_data=new, details=details)
