"""Settings (app_config) for the admin portal: list, one bulk save, and the change history
(MS-5091, MS-5046).

- The save is ONE call to ``admin_upsert_settings``: upsert + delete + one audit row (old/new values of
  the touched keys only) + APP_CACHE_KEY rotation when FORBIDDEN_COUNTRIES changed (created, edited or
  deleted), exactly when the portal rotated it before. All in one transaction.
- APP_CACHE_KEY can never be written or deleted here; secret-shaped keys (``is_secret_key`` of
  app/config/helper.py) can never be created or updated: secrets live in the environment only. A legacy
  secret row can still be deleted.
- History rows carry ``changes[]`` computed here, with secret-shaped values redacted.
"""
import json
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.exceptions.admin import AdminConflict, AdminNotFound, AdminValidationError
from app.repo.admin.app_config_repo import AppConfigAdminRepo
from app.repo.admin.audit_log_repo import AuditLogRepo
from app.repo.admin.base import AuditContext
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.settings import (NEW_KEY_PATTERN, AdminSettingChange, AdminSettingRow, AdminSettingsHistoryRow,
                                        AdminSettingsUpdate, AdminSettingsUpdateResult)
from app.services.admin.audit_query_service import (REDACTED, actor, client_reported, day_range, details_of,
                                                    is_secret_name, normalize_operation, users_for)

SETTINGS_TABLE = "app_config"


def _settings_map(value: Any) -> Dict[str, Any]:
    """``{key: value}`` from any stored shape. Raises ValueError / TypeError for an unreadable one.

    - None -> {}
    - a JSON string (legacy browser rows) -> parsed, then as below
    - a list of ``{key, value, ...}`` (legacy rows: the whole settings list before / after the save)
    - an app_config row snapshot ``{id, key, value, created_at}`` (generic audited helpers)
    - a ``{KEY: value}`` object (rows written by admin_upsert_settings)
    """
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
        if value is None:
            return {}
    if isinstance(value, list):
        out: Dict[str, Any] = {}
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                raise ValueError("settings item without a key")
            out[item["key"]] = item.get("value")
        return out
    if isinstance(value, dict):
        if isinstance(value.get("key"), str) and "value" in value and "id" in value:
            return {value["key"]: value["value"]}
        return dict(value)
    raise TypeError(f"unexpected settings data: {type(value).__name__}")


def compute_changes(old_data: Any, new_data: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """``(changes, parse_error)``: the keys whose value differs between old and new, sorted by key, as
    ``{key, old_value, new_value}``. A created key has ``old_value`` None, a deleted one ``new_value``
    None. Values are NOT redacted here (see :func:`redact_changes`). An unreadable side gives
    ``([], True)``."""
    try:
        old_map = _settings_map(old_data)
        new_map = _settings_map(new_data)
    except (ValueError, TypeError):
        return [], True
    changes = []
    for key in sorted(set(old_map) | set(new_map)):
        if (key in old_map) != (key in new_map) or old_map.get(key) != new_map.get(key):
            changes.append({"key": key, "old_value": old_map.get(key), "new_value": new_map.get(key)})
    return changes, False


def redact_changes(changes: List[Dict[str, Any]]) -> List[AdminSettingChange]:
    """Secret-shaped keys get ``"***"`` for every non-null value and ``redacted = true``."""
    out = []
    for change in changes:
        if is_secret_name(change["key"]):
            out.append(AdminSettingChange(
                key=change["key"],
                old_value=None if change["old_value"] is None else REDACTED,
                new_value=None if change["new_value"] is None else REDACTED,
                redacted=True))
        else:
            out.append(AdminSettingChange(**change))
    return out


def history_row(row: Dict[str, Any], users: Dict[str, Dict[str, Any]]) -> AdminSettingsHistoryRow:
    details = details_of(row)
    changes, parse_error = compute_changes(row.get("old_data"), row.get("new_data"))
    operation, _ = normalize_operation(row.get("operation"), details)
    ip = details.get("ip_address")
    reason = details.get("reason")
    return AdminSettingsHistoryRow(
        id=row["id"],
        changed_at=row.get("changed_at"),
        changed_by=actor(row.get("changed_by"), users),
        ip_address=ip if isinstance(ip, str) and ip.strip() else None,
        client_reported=client_reported(details),
        reason=reason if isinstance(reason, str) and reason.strip() else None,
        operation=operation or None,
        changes=redact_changes(changes),
        parse_error=parse_error,
    )


class SettingsAdminService:
    def __init__(self, settings: Optional[AppConfigAdminRepo] = None, audit: Optional[AuditLogRepo] = None,
                 users: Optional[UsersCopyAdminRepo] = None):
        self.settings = settings or AppConfigAdminRepo()
        self.audit = audit or AuditLogRepo()
        self.users = users or UsersCopyAdminRepo()

    # ---------- list ----------

    def list(self, page: PageParams, *, key: Optional[str] = None) -> AdminPage:
        result = self.settings.search(page, key=key)
        rows = []
        for row in result.rows:
            secret = is_secret_name(row.get("key"))
            rows.append(AdminSettingRow(id=row.get("id"), key=row.get("key") or "",
                                   value=REDACTED if secret and row.get("value") is not None else row.get("value"),
                                   created_at=row.get("created_at"), is_secret=secret))
        return AdminPage(data=rows, total_count=result.total)

    # ---------- bulk save ----------

    def update(self, body: AdminSettingsUpdate, ctx: AuditContext) -> AdminSettingsUpdateResult:
        """One save. 422 for a new key that is not ``^[A-Z0-9_]{1,100}$``; 409 ``no_change`` when no
        value differs and no listed key to delete exists (nothing is written or audited)."""
        current = self.settings.by_keys([item.key for item in body.items] + list(body.deleted_keys))
        for index, item in enumerate(body.items):
            if item.key not in current and not NEW_KEY_PATTERN.match(item.key):
                raise AdminValidationError(
                    f"items.{index}.key: invalid_key, a new key must be upper case letters, digits and _ "
                    f"(1 to 100 characters)")
        changed = [item for item in body.items
                   if item.key not in current or current[item.key].get("value") != item.value]
        deleting = [key for key in body.deleted_keys if key in current]
        if not changed and not deleting:
            raise AdminConflict("no_change")
        result = self.settings.upsert_settings(
            [{"key": item.key, "value": item.value} for item in changed], deleting, ctx)
        if isinstance(result, list):  # a scalar jsonb RPC result can arrive wrapped in a list
            result = result[0] if result else {}
        result = result or {}
        return AdminSettingsUpdateResult(updated=list(result.get("updated") or []),
                                    deleted=list(result.get("deleted") or []),
                                    cache_key_rotated=bool(result.get("cache_key_rotated")))

    # ---------- history ----------

    def history(self, page: PageParams, *, key: Optional[str] = None, changed_by: Optional[str] = None,
                date_from: Optional[date] = None, date_to: Optional[date] = None) -> AdminPage:
        start, end = day_range(date_from, date_to)
        result = self.audit.search(page, table=SETTINGS_TABLE, changed_by=changed_by, changed_from=start,
                                   changed_to=end, setting_key=key)
        users = users_for(result.rows, self.users)
        return AdminPage(data=[history_row(row, users) for row in result.rows], total_count=result.total)

    def history_one(self, audit_id: int) -> AdminSettingsHistoryRow:
        row = self.audit.get(audit_id)
        if row is None or row.get("table_name") != SETTINGS_TABLE:
            raise AdminNotFound("not_found")
        return history_row(row, users_for([row], self.users))
