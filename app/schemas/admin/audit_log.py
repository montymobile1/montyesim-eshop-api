"""Shapes of GET /api/v1/admin/audit-log (MS-5048). Read only: there is no write route."""
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel

from app.schemas.admin.common import AdminPage
from app.schemas.admin.settings import AdminActor


class AdminAuditTable(str, Enum):
    bundle = "bundle"
    app_config = "app_config"
    auth_users = "auth.users"
    tag_group = "tag_group"
    promotion = "promotion"
    voucher = "voucher"
    banner = "banner"
    promotion_rule = "promotion_rule"
    bundle_tag = "bundle_tag"
    tag = "tag"


class AdminAuditOperation(str, Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    BAN = "BAN"
    UNBAN = "UNBAN"
    BULK = "BULK"


class AdminAuditLogRow(BaseModel):
    """One audit row as listed. ``operation`` is normalised (legacy browser values such as "Ban User" or
    "Activate Bundle" are mapped, the original text is kept in ``legacy_operation``). ``reason``,
    ``rows_affected`` and ``ip_address`` come from operation_details. ``client_reported`` is true for rows
    written by the browser before the admin API (their IP was reported by the browser)."""
    id: int
    changed_at: Optional[datetime] = None
    table_name: str
    operation: str
    legacy_operation: Optional[str] = None
    record_id: Optional[str] = None
    changed_by: Optional[AdminActor] = None
    ip_address: Optional[str] = None
    reason: Optional[str] = None
    rows_affected: Optional[int] = None
    client_reported: bool
    summary: Optional[str] = None


class AdminAuditLogDetail(AdminAuditLogRow):
    """The row plus its data. Values of secret-shaped keys are ``"***"``. ``parse_error`` is true when a
    legacy row's old_data/new_data could not be read (the raw text is then not returned)."""
    hours_banned: Optional[float] = None
    old_data: Optional[Any] = None
    new_data: Optional[Any] = None
    operation_details: Optional[Dict[str, Any]] = None
    parse_error: bool = False


class AdminAuditLogPage(AdminPage[AdminAuditLogRow]):
    """List ``data``; ``facets.by_table`` counts rows per table over every filter except ``table``."""
    facets: Optional[Dict[str, Dict[str, int]]] = None
