"""Admin API: promotions. Owned by MS-5050 (list, status, delete), MS-5052 (get, create, edit), MS-5054 (usage).

``/usage`` is declared before ``/{promotion_id}`` so it is never read as an id.
"""
from datetime import date
from typing import List, Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.exceptions.admin import AdminValidationError
from app.schemas.admin.common import AdminStatusPatch, PageParams, ok, ok_page, page_params
from app.schemas.admin.promotions import (AdminPromotion, AdminPromotionCreate, AdminPromotionPage,
                                          AdminPromotionUpdate, AdminPromotionUsagePage, PromotionStatus)
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.promotion_service import PromotionAdminService

router = admin_router()

service = PromotionAdminService()

READ = "admin.promotions.read"
WRITE = "admin.promotions.write"

PromotionId = Path(..., description="promotion id (uuid)")


@router.get("", response_model=Response[AdminPromotionPage])
def list_promotions(search: Optional[str] = Query(None, max_length=100, description="part of the name or code"),
                    status: PromotionStatus = Query("all"),
                    rule_ids: Optional[List[UUID]] = Query(None),
                    bundle_codes: Optional[List[str]] = Query(None, description="any of these bundle codes"),
                    include_global: bool = Query(False, description="with bundle_codes: also the promotions "
                                                                    "that apply to every bundle"),
                    page: PageParams = Depends(page_params),
                    user: AdminUser = Depends(require_permission(READ))) -> Response:
    """Promotions, newest first, with their rule; `facets:{all,active,inactive,expired,scheduled}` over the
    searched set. `expired` = valid_to in the past, `scheduled` = valid_from in the future; `active` /
    `inactive` = `is_active` of the promotions in their validity window."""
    codes = [code.strip() for code in (bundle_codes or []) if code and code.strip()]
    for code in codes:
        if len(code) > 100:
            raise AdminValidationError("bundle_codes: a code is at most 100 characters")
    return ok_page(service.list(page, search=search, status=status,
                                rule_ids=[str(r) for r in rule_ids] if rule_ids else None,
                                bundle_codes=codes or None, include_global=include_global))


@router.get("/usage", response_model=Response[AdminPromotionUsagePage],
            summary="Promotion usage with totals over every matching row (totals ignore paging)")
def list_usage(code: Optional[str] = Query(None, max_length=100, description="part of the promotion code"),
               user_id: Optional[UUID] = Query(None),
               bundle_ids: Optional[List[UUID]] = Query(None),
               date_from: Optional[date] = Query(None, alias="from", description="First day (UTC), inclusive"),
               date_to: Optional[date] = Query(None, alias="to", description="Last day (UTC), inclusive; "
                                                                             "at most 366 days after from"),
               page: PageParams = Depends(page_params),
               user: AdminUser = Depends(require_permission(READ))) -> Response:
    """`totals:{uses, distinct_users, discount_amount:[{currency, amount}]}` are computed in SQL over every
    row the filters select, not over the page. Rows whose promotion or user was deleted are still listed."""
    return ok_page(service.usage(page, code=code, user_id=str(user_id) if user_id else None,
                                 bundle_ids=[str(b) for b in bundle_ids] if bundle_ids else None,
                                 date_from=date_from, date_to=date_to))


@router.get("/{promotion_id}", response_model=Response[AdminPromotion])
def get_promotion(promotion_id: UUID = PromotionId,
                  user: AdminUser = Depends(require_permission(READ))) -> Response:
    """One promotion with its rule and `bundle_codes` as an array. 404."""
    return ok(service.get(str(promotion_id)))


@router.post("", status_code=201, response_model=Response[AdminPromotion])
def create_promotion(body: AdminPromotionCreate, request: Request,
                     user: AdminUser = Depends(require_permission(WRITE))) -> Response:
    """201. 409 `duplicate_code` / `duplicate_name`; 422 unknown rule, missing amount, unknown bundle code."""
    response = ok(service.create(body, audit_context(user, request)))
    response.responseCode = 201
    return response


@router.put("/{promotion_id}", response_model=Response[AdminPromotion])
def update_promotion(body: AdminPromotionUpdate, request: Request, promotion_id: UUID = PromotionId,
                     user: AdminUser = Depends(require_permission(WRITE))) -> Response:
    """200. 404; 409 `duplicate_code` / `duplicate_name`; 409 `promotion_used` {times_used, fields} when
    code, rule, amount or valid_from change on a used promotion; 422 unknown bundle code."""
    return ok(service.update(str(promotion_id), body, audit_context(user, request)))


@router.patch("/{promotion_id}/status", response_model=Response[AdminPromotion])
def set_promotion_status(body: AdminStatusPatch, request: Request, promotion_id: UUID = PromotionId,
                         user: AdminUser = Depends(require_permission(WRITE))) -> Response:
    """Activate or deactivate. 404; 409 `no_change` when already in that state."""
    return ok(service.set_status(str(promotion_id), body, audit_context(user, request, body.reason)))


@router.delete("/{promotion_id}", status_code=204, response_class=HttpResponse)
def delete_promotion(request: Request, promotion_id: UUID = PromotionId,
                     user: AdminUser = Depends(require_permission(WRITE))) -> HttpResponse:
    """204. 404 unknown id; 409 `promotion_used` {times_used} when the promotion was used."""
    service.delete(str(promotion_id), audit_context(user, request))
    return HttpResponse(status_code=204)
