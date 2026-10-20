"""Admin API: vouchers. Owned by MS-5089."""
from typing import Optional

from fastapi import Depends, Path, Query, Request
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import AdminPage, PageParams, AdminStatusPatch, ok, ok_page, page_params
from app.schemas.admin.vouchers import AdminVoucher, AdminVoucherCreate, VoucherStatus
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.voucher_service import VoucherAdminService

router = admin_router()

service = VoucherAdminService()

VoucherId = Path(..., ge=1, description="voucher id")


@router.get("", response_model=Response[AdminPage[AdminVoucher]])
def list_vouchers(search: Optional[str] = Query(None, max_length=100, description="part of the code"),
                  status: VoucherStatus = Query("all"),
                  page: PageParams = Depends(page_params),
                  user: AdminUser = Depends(require_permission("admin.vouchers.read"))) -> Response:
    """Vouchers, newest first, with the user who used each one and `facets:{all,active,inactive}`."""
    return ok_page(service.list(search, status, page))


@router.post("", status_code=201, response_model=Response[AdminVoucher])
def create_voucher(body: AdminVoucherCreate, request: Request,
                   user: AdminUser = Depends(require_permission("admin.vouchers.write"))) -> Response:
    """201. 409 `duplicate_code` when the code exists in any letter case."""
    response = ok(service.create(body, audit_context(user, request)))
    response.responseCode = 201
    return response


@router.patch("/{voucher_id}/status", response_model=Response[AdminVoucher])
def set_voucher_status(body: AdminStatusPatch, request: Request, voucher_id: int = VoucherId,
                       user: AdminUser = Depends(require_permission("admin.vouchers.write"))) -> Response:
    """Activate or deactivate. 404; 409 `no_change` when already in that state."""
    return ok(service.set_status(voucher_id, body, audit_context(user, request, body.reason)))


@router.delete("/{voucher_id}", status_code=204, response_class=HttpResponse)
def delete_voucher(request: Request, voucher_id: int = VoucherId,
                   user: AdminUser = Depends(require_permission("admin.vouchers.write"))) -> HttpResponse:
    """204. 404 unknown id; 409 `voucher_used` when the voucher was already redeemed."""
    service.delete(voucher_id, audit_context(user, request))
    return HttpResponse(status_code=204)
