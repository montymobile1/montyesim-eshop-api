"""Admin API: audit_log. Owned by MS-5048. Read only (no route writes or deletes audit rows); super admin only."""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import Depends, Path, Query

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.audit_log import AdminAuditLogDetail, AdminAuditLogPage, AdminAuditOperation, AdminAuditTable
from app.schemas.admin.common import PageParams, ok, ok_page, page_params
from app.schemas.response import Response
from app.services.admin.audit_query_service import AuditQueryService

router = admin_router()
service = AuditQueryService()

READ = "admin.audit.read"


@router.get("", response_model=Response[AdminAuditLogPage], summary="Audit trail, newest first")
def list_audit_log(page: PageParams = Depends(page_params),
                   table: Optional[AdminAuditTable] = Query(None),
                   operation: Optional[AdminAuditOperation] = Query(
                       None, description="Legacy browser operations are mapped: 'Ban User' -> BAN, "
                                         "'Unban User' -> UNBAN, 'Activate/Deactivate Bundle' -> UPDATE "
                                         "(BULK when more than one row)"),
                   changed_by: Optional[UUID] = Query(None, description="Admin user id"),
                   date_from: Optional[date] = Query(None, alias="from", description="First day (UTC), inclusive"),
                   date_to: Optional[date] = Query(None, alias="to", description="Last day (UTC), inclusive"),
                   user: AdminUser = Depends(require_permission(READ))):
    """``facets.by_table`` counts rows per table over every filter except ``table``. ``client_reported`` is
    true for legacy rows written by the browser (their IP, when present, was reported by the browser)."""
    return ok_page(service.list(page, table=table.value if table else None,
                                operation=operation.value if operation else None,
                                changed_by=str(changed_by) if changed_by else None,
                                date_from=date_from, date_to=date_to))


@router.get("/{audit_id}", response_model=Response[AdminAuditLogDetail], summary="One audit row with its data")
def get_audit_log(audit_id: int = Path(..., ge=1),
                  user: AdminUser = Depends(require_permission(READ))):
    """old_data / new_data parsed (legacy rows store JSON strings) with secret-shaped keys redacted."""
    return ok(service.get(audit_id))
