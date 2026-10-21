"""Admin API: settings. Owned by MS-5091 (list, bulk save) and MS-5046 (history). Super admin only."""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import AdminPage, PageParams, ok, ok_page, page_params
from app.schemas.admin.settings import AdminSettingRow, AdminSettingsHistoryRow, AdminSettingsUpdate, AdminSettingsUpdateResult
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.settings_service import SettingsAdminService

router = admin_router()
service = SettingsAdminService()

READ = "admin.settings.read"
WRITE = "admin.settings.write"


@router.get("", response_model=Response[AdminPage[AdminSettingRow]], summary="List settings, newest first")
def list_settings(page: PageParams = Depends(page_params),
                  key: Optional[str] = Query(None, min_length=1, max_length=100, description="Exact key"),
                  user: AdminUser = Depends(require_permission(READ))):
    """Secret-shaped keys left in app_config are listed with ``value = "***"`` and ``is_secret = true``."""
    return ok_page(service.list(page, key=key))


@router.put("", response_model=Response[AdminSettingsUpdateResult], summary="Save settings in one transaction")
def update_settings(body: AdminSettingsUpdate, request: Request,
                    user: AdminUser = Depends(require_permission(WRITE))):
    """Creates / updates ``items`` and deletes ``deleted_keys`` in one transaction with ONE audit row
    (old and new values of the touched keys only). APP_CACHE_KEY is rotated by the server when
    FORBIDDEN_COUNTRIES changed (``cache_key_rotated``). Only changed values are written.

    Errors: 409 ``no_change`` when nothing differs; 422 when APP_CACHE_KEY is in either list, an item key is
    secret-shaped, a key is repeated or in both lists, both lists are empty, a new key is not
    ``^[A-Z0-9_]{1,100}$`` or a value is longer than 10 000 characters."""
    return ok(service.update(body, audit_context(user, request, body.reason)))


@router.get("/history", response_model=Response[AdminPage[AdminSettingsHistoryRow]],
            summary="Settings change history, newest first")
def settings_history(page: PageParams = Depends(page_params),
                     key: Optional[str] = Query(None, min_length=1, max_length=100,
                                                description="Only saves that changed this key"),
                     changed_by: Optional[UUID] = Query(None, description="Admin user id"),
                     date_from: Optional[date] = Query(None, alias="from", description="First day (UTC), inclusive"),
                     date_to: Optional[date] = Query(None, alias="to", description="Last day (UTC), inclusive"),
                     user: AdminUser = Depends(require_permission(READ))):
    """Each row lists only the keys whose value changed (``changes[]``, computed on the server).
    Redaction rule: values of keys matching SECRET|PASSWORD|TOKEN|PRIVATE|API_KEY, or the secret rule of the
    configuration (``*_KEY``, ``*_SECRET``, ``*_PASSWORD``, ``*_TOKEN``, ``*_USERNAME``, ``RESELLER_ID``), are
    returned as ``"***"`` with ``redacted = true``, in every row, old and new; APP_CACHE_KEY is not a secret.
    A legacy row that cannot be read has ``changes: []`` and ``parse_error: true``."""
    return ok_page(service.history(page, key=key, changed_by=str(changed_by) if changed_by else None,
                                   date_from=date_from, date_to=date_to))


@router.get("/history/{audit_id}", response_model=Response[AdminSettingsHistoryRow], summary="One settings save")
def settings_history_one(audit_id: int = Path(..., ge=1),
                         user: AdminUser = Depends(require_permission(READ))):
    return ok(service.history_one(audit_id))
