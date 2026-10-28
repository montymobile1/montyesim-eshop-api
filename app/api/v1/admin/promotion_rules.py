"""Admin API: promotion_rules. Owned by MS-5077 (lookup), MS-5087 (rules page)."""
from typing import List, Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._lookup import add_lookup_route
from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import AdminPage, PageParams, ok, ok_page, page_params
from app.schemas.admin.promotion_rules import AdminPromotionRule, AdminPromotionRuleBody, AdminPromotionRuleRef
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.promotion_rule_service import PromotionRuleAdminService

router = admin_router()

# MS-5077: GET /lookup. Keep it above any /{id} route.
add_lookup_route(router, "promotion_rules", "admin.rules.read")

service = PromotionRuleAdminService()

READ = "admin.rules.read"
WRITE = "admin.rules.write"

RuleId = Path(..., description="promotion rule id (uuid)")


@router.get("/actions", response_model=Response[List[AdminPromotionRuleRef]], summary="Rule actions ({id, name})")
def list_actions(user: AdminUser = Depends(require_permission(READ))) -> Response:
    return ok(service.actions())


@router.get("/events", response_model=Response[List[AdminPromotionRuleRef]], summary="Rule events ({id, name})")
def list_events(user: AdminUser = Depends(require_permission(READ))) -> Response:
    return ok(service.events())


@router.get("", response_model=Response[AdminPage[AdminPromotionRule]])
def list_rules(search: Optional[str] = Query(None, max_length=100, description="part of the rule id"),
               action_id: Optional[int] = Query(None, ge=1),
               event_id: Optional[int] = Query(None, ge=1),
               page: PageParams = Depends(page_params),
               user: AdminUser = Depends(require_permission(READ))) -> Response:
    """Rules, newest first, with their action and event."""
    return ok_page(service.list(search, action_id, event_id, page))


@router.post("", status_code=201, response_model=Response[AdminPromotionRule])
def create_rule(body: AdminPromotionRuleBody, request: Request,
                user: AdminUser = Depends(require_permission(WRITE))) -> Response:
    """201. 409 `duplicate_rule` for an existing (action, event, max_usage); 422 unknown action or event."""
    response = ok(service.create(body, audit_context(user, request)))
    response.responseCode = 201
    return response


@router.put("/{rule_id}", response_model=Response[AdminPromotionRule])
def update_rule(body: AdminPromotionRuleBody, request: Request, rule_id: UUID = RuleId,
                user: AdminUser = Depends(require_permission(WRITE))) -> Response:
    """200. Only `beneficiary` changes; action, event and max_usage must repeat the stored values, else
    422 `rule_field_locked` with `fields` (TEOS-212). 404 unknown id."""
    return ok(service.update(str(rule_id), body, audit_context(user, request)))


@router.delete("/{rule_id}", status_code=204, response_class=HttpResponse)
def delete_rule(request: Request, rule_id: UUID = RuleId,
                user: AdminUser = Depends(require_permission(WRITE))) -> HttpResponse:
    """204. 404 unknown id; 409 `rule_in_use` when a promotion references the rule."""
    service.delete(str(rule_id), audit_context(user, request))
    return HttpResponse(status_code=204)
