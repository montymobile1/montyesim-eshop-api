"""Admin API: groups. Owned by MS-5077 (lookup) and MS-5085 (list, detail, add, edit, delete, status, tag order).

Create and edit are multipart/form-data: a ``payload`` field with the JSON body (``AdminGroupCreate`` /
``AdminGroupUpdate``) and one file per icon named ``icon_<client_key>`` (new tags) or ``icon_<tag id>``
(existing tags). Any other form field is 422. The permission is checked before any file is read.
"""
import json
from typing import Dict, Optional, Tuple, Type, TypeVar

from fastapi import Depends, Path, Query, Request
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._lookup import add_lookup_route
from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.exceptions.admin import AdminValidationError
from app.schemas.admin.common import AdminPage, AdminStatusPatch, PageParams, ok, ok_page, page_params
from app.schemas.admin.groups import (ICON_FIELD_PREFIX, PAYLOAD_FIELD, AdminGroup, AdminGroupCreate,
                                      AdminGroupDetail, AdminGroupStatus, AdminGroupUpdate, AdminTag,
                                      AdminTagOrder, AdminTagOrderResult, GroupStatus)
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.group_service import GroupAdminService

router = admin_router()

# MS-5077: GET /lookup. Keep it above any /{id} route.
add_lookup_route(router, "groups", "admin.groups.read")

service = GroupAdminService()

GroupId = Path(..., ge=1, description="group id")

M = TypeVar("M", bound=BaseModel)


def _validation_text(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(piece) for piece in error.get("loc", ()))
        parts.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    return "; ".join(parts) or "invalid payload"


async def _group_form(request: Request, model: Type[M]) -> Tuple[M, Dict[str, bytes]]:
    """``payload`` parsed as ``model`` plus ``{client_key: bytes}`` of the ``icon_*`` files."""
    form = await request.form()
    payload: Optional[str] = None
    files: Dict[str, bytes] = {}
    unknown = []
    for key, value in form.multi_items():
        if key == PAYLOAD_FIELD and isinstance(value, str):
            payload = value
        elif key.startswith(ICON_FIELD_PREFIX) and len(key) > len(ICON_FIELD_PREFIX) and isinstance(value, UploadFile):
            files[key[len(ICON_FIELD_PREFIX):]] = service.read_image(value.file)
        else:
            unknown.append(key)
    if unknown:
        raise AdminValidationError(f"unknown field(s): {', '.join(sorted(set(unknown)))}")
    if payload is None:
        raise AdminValidationError(f"{PAYLOAD_FIELD}: required (JSON text)")
    try:
        parsed = model.model_validate_json(payload)
    except ValidationError as exc:
        raise AdminValidationError(f"{PAYLOAD_FIELD}: {_validation_text(exc)}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raise AdminValidationError(f"{PAYLOAD_FIELD}: not valid JSON")
    return parsed, files


async def create_form(request: Request) -> Tuple[AdminGroupCreate, Dict[str, bytes]]:
    return await _group_form(request, AdminGroupCreate)


async def update_form(request: Request) -> Tuple[AdminGroupUpdate, Dict[str, bytes]]:
    return await _group_form(request, AdminGroupUpdate)


@router.get("", response_model=Response[AdminPage[AdminGroup]])
def list_groups(search: Optional[str] = Query(None, max_length=100, description="part of the group name"),
                status: GroupStatus = Query("all"),
                page: PageParams = Depends(page_params),
                user: AdminUser = Depends(require_permission("admin.groups.read"))) -> Response:
    """Groups, newest first, each with ``tag_count``; ``facets:{all,active,inactive}`` over the searched set."""
    return ok_page(service.list(search, status, page))


@router.get("/{group_id}", response_model=Response[AdminGroupDetail])
def get_group(group_id: int = GroupId,
              tag_search: Optional[str] = Query(None, max_length=100, description="part of a tag name"),
              user: AdminUser = Depends(require_permission("admin.groups.read"))) -> Response:
    """One group with its tags ordered by sorting_number then name (``tag_search`` filters them). 404 unknown id."""
    return ok(service.get(group_id, tag_search))


@router.get("/{group_id}/tags", response_model=Response[AdminPage[AdminTag]])
def list_group_tags(group_id: int = GroupId,
                    user: AdminUser = Depends(require_permission("admin.groups.read"))) -> Response:
    """Every tag of the group, ordered by sorting_number then name. 404 unknown group."""
    tags = service.tags(group_id)
    return ok_page(AdminPage(data=tags, total_count=len(tags)))


@router.post("", status_code=201, response_model=Response[AdminGroupDetail])
def create_group(request: Request,
                 user: AdminUser = Depends(require_permission("admin.groups.write")),
                 form: Tuple[AdminGroupCreate, Dict[str, bytes]] = Depends(create_form)) -> Response:
    """201 group with tags. 409 ``duplicate_group_name`` / ``duplicate_tag_name``; 413 icon too large; 422."""
    payload, files = form
    response = ok(service.create(payload, files, audit_context(user, request)))
    response.responseCode = 201
    return response


@router.put("/{group_id}", response_model=Response[AdminGroupDetail])
def update_group(request: Request, group_id: int = GroupId,
                 user: AdminUser = Depends(require_permission("admin.groups.write")),
                 form: Tuple[AdminGroupUpdate, Dict[str, bytes]] = Depends(update_form)) -> Response:
    """200 group with tags. 404; 409 ``duplicate_group_name`` / ``duplicate_tag_name`` / ``linked_bundles``
    (a deleted tag is assigned to a bundle); 422 tag of another group, unknown icon, bad payload."""
    payload, files = form
    return ok(service.update(group_id, payload, files, audit_context(user, request)))


@router.delete("/{group_id}", status_code=204, response_class=HttpResponse)
def delete_group(request: Request, group_id: int = GroupId,
                 user: AdminUser = Depends(require_permission("admin.groups.write"))) -> HttpResponse:
    """204, then the tag icons are removed from storage. 404 unknown id;
    409 ``{"code":"linked_bundles","bundle_count":n}`` when a tag is assigned to a bundle (nothing changes)."""
    service.delete(group_id, audit_context(user, request))
    return HttpResponse(status_code=204)


@router.patch("/{group_id}/status", response_model=Response[AdminGroupStatus])
def set_group_status(request: Request, body: AdminStatusPatch, group_id: int = GroupId,
                     user: AdminUser = Depends(require_permission("admin.groups.write"))) -> Response:
    """Activates or deactivates a group. 404; 409 ``no_change`` when already in that state."""
    return ok(service.set_status(group_id, body.is_active, audit_context(user, request, body.reason)))


@router.put("/{group_id}/tags/order", response_model=Response[AdminTagOrderResult])
def reorder_group_tags(request: Request, body: AdminTagOrder, group_id: int = GroupId,
                       user: AdminUser = Depends(require_permission("admin.groups.write"))) -> Response:
    """``tag_ids`` = every tag of the group in the wanted order; ``sorting_number`` becomes the position.
    404 unknown group; 422 when a tag is missing, repeated or belongs to another group."""
    return ok(service.reorder(group_id, body.tag_ids, audit_context(user, request)))
