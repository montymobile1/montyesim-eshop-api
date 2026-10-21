"""Admin API: banners. Owned by MS-5058 (list, delete) and MS-5060 (get, create, edit).

Create and edit are multipart/form-data: title, description, platform, action?, is_active?, image
(required on create, optional on edit). Any other form field is 422.
"""
from typing import Any, Dict, Optional

from fastapi import Depends, File, Form, Path, Query, Request, UploadFile
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.exceptions.admin import AdminValidationError
from app.schemas.admin.banners import (ACTION_PATTERN, BANNER_FORM_FIELDS, DESCRIPTION_MAX, IS_ACTIVE_PATTERN,
                                       TITLE_MAX, AdminBanner, BannerPlatform, BannerStatus)
from app.schemas.admin.common import AdminPage, PageParams, ok, ok_page, page_params
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.banner_service import BannerAdminService

router = admin_router()

service = BannerAdminService()

BannerId = Path(..., ge=1, description="banner id")


async def only_banner_fields(request: Request) -> None:
    """422 for a multipart field the banner form does not have (the form is already parsed and cached)."""
    form = await request.form()
    unknown = sorted({key for key in form.keys() if key not in BANNER_FORM_FIELDS})
    if unknown:
        raise AdminValidationError(f"unknown field(s): {', '.join(unknown)}")


def _fields(title: str, description: str, platform: str, action: Optional[str],
            is_active: Optional[str]) -> Dict[str, Any]:
    title, description = title.strip(), description.strip()
    if not title:
        raise AdminValidationError("title: must not be blank")
    if not description:
        raise AdminValidationError("description: must not be blank")
    fields: Dict[str, Any] = {"title": title, "description": description, "platform": platform,
                              "action": (action or "").strip() or None}
    if is_active is not None:
        fields["is_active"] = is_active.lower() == "true"
    return fields


@router.get("", response_model=Response[AdminPage[AdminBanner]])
def list_banners(search: Optional[str] = Query(None, max_length=100, description="part of the title"),
                 status: BannerStatus = Query("all"),
                 page: PageParams = Depends(page_params),
                 user: AdminUser = Depends(require_permission("admin.banners.read"))) -> Response:
    """Banners, newest first, with `facets:{all,active,inactive}` over the searched set."""
    return ok_page(service.list(search, status, page))


@router.get("/{banner_id}", response_model=Response[AdminBanner])
def get_banner(banner_id: int = BannerId,
               user: AdminUser = Depends(require_permission("admin.banners.read"))) -> Response:
    """One banner for the edit form. 404 unknown id."""
    return ok(service.get(banner_id))


@router.post("", status_code=201, response_model=Response[AdminBanner])
def create_banner(request: Request,
                  title: str = Form(..., max_length=TITLE_MAX),
                  description: str = Form(..., max_length=DESCRIPTION_MAX),
                  platform: BannerPlatform = Form(...),
                  action: Optional[str] = Form(None, pattern=ACTION_PATTERN),
                  is_active: Optional[str] = Form(None, pattern=IS_ACTIVE_PATTERN),
                  image: UploadFile = File(..., description="png, jpeg or webp"),
                  user: AdminUser = Depends(require_permission("admin.banners.write")),
                  _checked: None = Depends(only_banner_fields)) -> Response:
    """201. 409 `duplicate_title` (any letter case); 413 image too large; 422 invalid field or image."""
    fields = _fields(title, description, platform, action, is_active)
    data = service.read_image(image.file)
    response = ok(service.create(fields, data, audit_context(user, request)))
    response.responseCode = 201
    return response


@router.put("/{banner_id}", response_model=Response[AdminBanner])
def update_banner(request: Request,
                  banner_id: int = BannerId,
                  title: str = Form(..., max_length=TITLE_MAX),
                  description: str = Form(..., max_length=DESCRIPTION_MAX),
                  platform: BannerPlatform = Form(...),
                  action: Optional[str] = Form(None, pattern=ACTION_PATTERN),
                  is_active: Optional[str] = Form(None, pattern=IS_ACTIVE_PATTERN),
                  image: Optional[UploadFile] = File(None, description="new image; omit to keep the current one"),
                  user: AdminUser = Depends(require_permission("admin.banners.write")),
                  _checked: None = Depends(only_banner_fields)) -> Response:
    """200. 404; 409 `duplicate_title`; 409 `guard_failed` when another admin replaced the image meanwhile."""
    fields = _fields(title, description, platform, action, is_active)
    data = service.read_image(image.file) if image is not None else None
    return ok(service.update(banner_id, fields, data, audit_context(user, request)))


@router.delete("/{banner_id}", status_code=204, response_class=HttpResponse)
def delete_banner(request: Request, banner_id: int = BannerId,
                  user: AdminUser = Depends(require_permission("admin.banners.write"))) -> HttpResponse:
    """204, then the image file is removed from storage. 404 unknown id."""
    service.delete(banner_id, audit_context(user, request))
    return HttpResponse(status_code=204)
