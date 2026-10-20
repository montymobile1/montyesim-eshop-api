"""Read side of audit_log for the admin portal: the Audit log page (MS-5048) and the helpers the
Settings history (MS-5046) shares with it.

Rows come back raw from ``admin_audit_log_search``; everything the portal used to work out in the
browser is projected here: the actor email, ``reason`` / ``ip_address`` / ``rows_affected`` from
``operation_details``, the normalised operation, ``client_reported`` and a short summary.

Two generations of rows exist:
- legacy rows written by the browser: old_data / new_data are JSON *strings*, operation is free text
  ("Ban User", "Activate Bundle"...), operation_details has no ``client_reported`` and the IP (when
  present) was reported by the browser -> ``client_reported = true``;
- rows written by the admin API (``admin_write_audit``): objects, upper-case operations,
  ``client_reported = false`` and the IP read by the server.

Secret-shaped keys are redacted (``"***"``) everywhere, for every caller, super admins included.
"""
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, Optional, Tuple

from app.config.helper import is_secret_key
from app.exceptions.admin import AdminNotFound, AdminValidationError
from app.repo.admin.audit_log_repo import AuditLogRepo
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.schemas.admin.audit_log import AdminAuditLogDetail, AdminAuditLogPage, AdminAuditLogRow
from app.schemas.admin.common import PageParams
from app.schemas.admin.settings import AdminActor

REDACTED = "***"
# Plan rule (SECRET|PASSWORD|TOKEN|PRIVATE|API_KEY anywhere in the name) plus the consumer rule of
# app/config/helper.py (is_secret_key: *_KEY, *_SECRET, *_PASSWORD, *_TOKEN, *_USERNAME, RESELLER_ID).
_SECRET_NAME = re.compile(r"SECRET|PASSWORD|TOKEN|PRIVATE|API_KEY", re.IGNORECASE)
_NOT_SECRET = frozenset({"APP_CACHE_KEY", "CACHE_KEY"})

STANDARD_OPERATIONS = ("INSERT", "UPDATE", "DELETE", "BAN", "UNBAN", "BULK")
# Free-text operations written by the browser before the admin API. Same mapping as
# admin_audit_operation() in migrations/admin/110_audit_search.sql.
LEGACY_OPERATIONS = {
    "BAN USER": "BAN",
    "UNBAN USER": "UNBAN",
}
LEGACY_BUNDLE_STATUS = frozenset({"ACTIVATE BUNDLE", "DEACTIVATE BUNDLE"})

_LABEL_FIELDS = ("name", "title", "code", "bundle_name", "key", "email")


# ---------------------------------------------------------------- helpers shared with settings history

def is_secret_name(name: Any) -> bool:
    """True when a key or column name looks like a secret (APP_CACHE_KEY / CACHE_KEY excepted)."""
    if not isinstance(name, str) or not name:
        return False
    upper = name.upper()
    if upper in _NOT_SECRET:
        return False
    return bool(_SECRET_NAME.search(upper)) or is_secret_key(upper)


def redact(value: Any) -> Any:
    """A copy of ``value`` with the values of secret-shaped keys replaced by ``"***"``, at any depth.
    ``{"key": "SMTP_PASSWORD", "value": "x"}`` (an app_config row or a legacy settings item) is redacted
    on its ``value`` too."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[key] = REDACTED if (is_secret_name(key) and item is not None) else redact(item)
        if is_secret_name(value.get("key")) and out.get("value") is not None:
            out["value"] = REDACTED
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def parse_audit_json(value: Any) -> Tuple[Any, bool]:
    """``(parsed, parse_error)``: legacy rows hold JSON strings, new rows objects. A string that is not
    JSON gives ``(None, True)``; ``None`` gives ``(None, False)``."""
    if value is None:
        return None, False
    if isinstance(value, str):
        try:
            return json.loads(value), False
        except (TypeError, ValueError):
            return None, True
    return value, False


def details_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """operation_details as a dict; anything malformed becomes ``{}`` (projected fields are then null)."""
    parsed, _ = parse_audit_json(row.get("operation_details"))
    return parsed if isinstance(parsed, dict) else {}


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def normalize_operation(operation: Any, details: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """``(operation, legacy_operation)``. Standard values (any case) are upper-cased; legacy browser texts
    are mapped ("Ban User" -> BAN, "Unban User" -> UNBAN, "Activate/Deactivate Bundle" -> BULK when more
    than one row was affected, else UPDATE) and the original text is returned as ``legacy_operation``."""
    text = str(operation or "").strip()
    upper = text.upper()
    if upper in STANDARD_OPERATIONS:
        return upper, (None if text == upper else text)
    if upper in LEGACY_OPERATIONS:
        return LEGACY_OPERATIONS[upper], text
    if upper in LEGACY_BUNDLE_STATUS:
        rows = _int_or_none(details.get("rows_affected"))
        return ("BULK" if rows is not None and rows > 1 else "UPDATE"), text
    return text, None


def client_reported(details: Dict[str, Any]) -> bool:
    """Rows written by the admin API say ``client_reported: false``; older rows came from the browser."""
    flag = details.get("client_reported")
    return flag if isinstance(flag, bool) else True


def actor(user_id: Any, users: Dict[str, Dict[str, Any]]) -> Optional[AdminActor]:
    """``{id, email}`` of the admin, or None when the id is unknown (account deleted since)."""
    found = users.get(str(user_id)) if user_id else None
    return AdminActor(id=found["id"], email=found.get("email")) if found else None


def day_range(date_from: Optional[date], date_to: Optional[date]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """``from`` / ``to`` dates (inclusive, UTC days) as ``[start, end)`` timestamps; 422 when from > to."""
    if date_from and date_to and date_from > date_to:
        raise AdminValidationError("from: must be on or before to")
    start = datetime.combine(date_from, time.min) if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), time.min) if date_to else None
    return start, end


def users_for(rows: Iterable[Dict[str, Any]], users_repo: UsersCopyAdminRepo) -> Dict[str, Dict[str, Any]]:
    ids = [row.get("changed_by") for row in rows if row.get("changed_by")]
    return users_repo.by_ids(ids) if ids else {}


# ---------------------------------------------------------------- audit log page

def _label(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        for name in _LABEL_FIELDS:
            value = data.get(name)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                return str(value)
    return None


def summary_of(row: Dict[str, Any], operation: str, details: Dict[str, Any]) -> Optional[str]:
    """A one-line description for the list, built from data the row already has (never a secret value)."""
    from app.services.admin.settings_service import compute_changes  # local import: avoids an import cycle

    table = row.get("table_name")
    if table == "app_config" and row.get("record_id") is None and not details.get("record_id"):
        changes, parse_error = compute_changes(row.get("old_data"), row.get("new_data"))
        if parse_error:
            return "Settings changed (details unavailable)"
        keys = [change["key"] for change in changes]
        if not keys:
            return "No setting changed"
        shown = ", ".join(keys[:3]) + (f" and {len(keys) - 3} more" if len(keys) > 3 else "")
        return f"{len(keys)} setting(s) changed: {shown}"
    if operation == "BAN":
        hours = _float_or_none(details.get("hours_banned"))
        return f"Banned for {hours:g} hours" if hours is not None else "Banned"
    if operation == "UNBAN":
        return "Unbanned"
    rows = _int_or_none(details.get("rows_affected"))
    if rows is not None and rows > 1:
        return f"{rows} rows"
    new_data, _ = parse_audit_json(row.get("new_data"))
    old_data, _ = parse_audit_json(row.get("old_data"))
    label = _label(new_data) or _label(old_data)
    if label and not is_secret_name(label):
        return label
    return None


def project_row(row: Dict[str, Any], users: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The list projection of one raw audit row (the fields of :class:`AdminAuditLogRow`)."""
    details = details_of(row)
    operation, legacy_operation = normalize_operation(row.get("operation"), details)
    record_id = row.get("record_id") or details.get("record_id")
    return {
        "id": row.get("id"),
        "changed_at": row.get("changed_at"),
        "table_name": row.get("table_name") or "",
        "operation": operation,
        "legacy_operation": legacy_operation,
        "record_id": None if record_id is None else str(record_id),
        "changed_by": actor(row.get("changed_by"), users),
        "ip_address": _str_or_none(details.get("ip_address")),
        "reason": _str_or_none(details.get("reason")),
        "rows_affected": _int_or_none(details.get("rows_affected")),
        "client_reported": client_reported(details),
        "summary": summary_of(row, operation, details),
    }


class AuditQueryService:
    def __init__(self, audit: Optional[AuditLogRepo] = None, users: Optional[UsersCopyAdminRepo] = None):
        self.audit = audit or AuditLogRepo()
        self.users = users or UsersCopyAdminRepo()

    def list(self, page: PageParams, *, table: Optional[str] = None, operation: Optional[str] = None,
             changed_by: Optional[str] = None, date_from: Optional[date] = None,
             date_to: Optional[date] = None) -> AdminAuditLogPage:
        start, end = day_range(date_from, date_to)
        result = self.audit.search(page, table=table, operation=operation, changed_by=changed_by,
                                   changed_from=start, changed_to=end)
        users = users_for(result.rows, self.users)
        rows = [AdminAuditLogRow(**project_row(row, users)) for row in result.rows]
        return AdminAuditLogPage(data=rows, total_count=result.total,
                            facets={"by_table": {str(k): int(v) for k, v in result.facets_by_table.items()}})

    def get(self, audit_id: int) -> AdminAuditLogDetail:
        row = self.audit.get(audit_id)
        if row is None:
            raise AdminNotFound("not_found")
        users = users_for([row], self.users)
        details = details_of(row)
        old_data, old_error = parse_audit_json(row.get("old_data"))
        new_data, new_error = parse_audit_json(row.get("new_data"))
        return AdminAuditLogDetail(
            **project_row(row, users),
            hours_banned=_float_or_none(details.get("hours_banned")),
            old_data=redact(old_data),
            new_data=redact(new_data),
            operation_details=redact(details) or None,
            parse_error=old_error or new_error,
        )
