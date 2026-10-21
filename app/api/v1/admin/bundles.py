"""Admin API: bundles. Owned by MS-5077 (lookup) and MS-5083 (bundles page, assign to groups)."""
from typing import List, Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request

from app.api.v1.admin._lookup import add_lookup_route
from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.bundles import (
    AdminBundle,
    AdminBundleBulkResult,
    AdminBundleBulkStatus,
    AdminBundleDetail,
    AdminBundleRename,
    AdminBundleRenamed,
    AdminBundleStatus,
    AdminBundleStatusPatch,
    AdminBundleTags,
    AdminBundleTagsSet,
    BundleStatus,
)
from app.schemas.admin.common import AdminPage, PageParams, ok, ok_page, page_params
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.bundle_service import BundleAdminService

router = admin_router()

service = BundleAdminService()

# MS-5077: GET /lookup. Keep it above any /{id} route.
add_lookup_route(router, "bundles", "admin.bundles.read")

BundleId = Path(..., description="bundle id (uuid)")


@router.get("", response_model=Response[AdminPage[AdminBundle]])
def list_bundles(search: Optional[str] = Query(None, max_length=100,
                                               description="part of the name, display name, code or id"),
                 tag_ids: Optional[List[UUID]] = Query(None, description="repeat the key for several tags"),
                 status: BundleStatus = Query("all"),
                 page: PageParams = Depends(page_params),
                 user: AdminUser = Depends(require_permission("admin.bundles.read"))) -> Response:
    """Bundles, newest first. `facets:{all,active,inactive}` count the searched set;
    `totals:{total,active_count,inactive_count}` the whole table (for the bulk buttons)."""
    ids = [str(t) for t in tag_ids] if tag_ids else None
    return ok_page(service.list(search, ids, status, page))


# Before the /{bundle_id} routes, so "status" is never read as an id.
@router.post("/status/bulk", response_model=Response[AdminBundleBulkResult])
def set_all_bundles_status(body: AdminBundleBulkStatus, request: Request,
                           user: AdminUser = Depends(require_permission("admin.bundles.bulk"))) -> Response:
    """Activate or deactivate every bundle (super admin). 409 `no_change` when none would change."""
    return ok(service.set_all_status(body, audit_context(user, request, body.reason)))


@router.get("/{bundle_id}", response_model=Response[AdminBundleDetail])
def get_bundle(bundle_id: UUID = BundleId,
               user: AdminUser = Depends(require_permission("admin.bundles.read"))) -> Response:
    """The bundle with its tags and their groups. 404 unknown id; 422 not a uuid."""
    return ok(service.get(str(bundle_id)))


@router.patch("/{bundle_id}", response_model=Response[AdminBundleRenamed])
def rename_bundle(body: AdminBundleRename, request: Request, bundle_id: UUID = BundleId,
                  user: AdminUser = Depends(require_permission("admin.bundles.write"))) -> Response:
    """Display name (1 to 60 characters). 404; 409 `no_change` when it is already that name."""
    return ok(service.rename(str(bundle_id), body, audit_context(user, request)))


@router.patch("/{bundle_id}/status", response_model=Response[AdminBundleStatus])
def set_bundle_status(body: AdminBundleStatusPatch, request: Request, bundle_id: UUID = BundleId,
                      user: AdminUser = Depends(require_permission("admin.bundles.bulk"))) -> Response:
    """Activate or deactivate one bundle; a reason is required to deactivate. 404; 409 `no_change`.

    Super admin only (`admin.bundles.bulk`), as the portal shows the row toggle today."""
    return ok(service.set_status(str(bundle_id), body, audit_context(user, request, body.reason)))


@router.put("/{bundle_id}/tags", response_model=Response[AdminBundleTags])
def set_bundle_tags(body: AdminBundleTagsSet, request: Request, bundle_id: UUID = BundleId,
                    user: AdminUser = Depends(require_permission("admin.bundles.write"))) -> Response:
    """Replaces the bundle's tag set; an empty list removes every tag. 404; 422 unknown tag."""
    return ok(service.set_tags(str(bundle_id), body, audit_context(user, request)))
