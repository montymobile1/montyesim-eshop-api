"""Admin API: referrals. Owned by MS-5056."""
from fastapi import Depends, Request
from starlette.responses import StreamingResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import ERROR_ENVELOPE, AdminPage, PageParams, ok_page, page_params
from app.schemas.admin.referrals import (AdminReferral, AdminReferralExportRequest, AdminReferralFilters,
                                         referral_filters)
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.referral_service import ReferralAdminService

router = admin_router()

service = ReferralAdminService()


@router.get("", response_model=Response[AdminPage[AdminReferral]])
def list_referrals(filters: AdminReferralFilters = Depends(referral_filters),
                   page: PageParams = Depends(page_params),
                   user: AdminUser = Depends(require_permission("admin.referrals.read"))) -> Response:
    """Referral transactions, newest first, with the referrer and the referred user joined.
    Filters: `user_id` (the referred user), `search` (part of the code), `from_email`, `date`."""
    return ok_page(service.list(filters, page))


@router.post("/export", response_class=StreamingResponse, responses={
    200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
          "description": "The matching rows as an .xlsx attachment"},
    422: {"content": ERROR_ENVELOPE,
          "description": "Invalid filters, or more rows than ADMIN_EXPORT_ROW_CAP (`too_many_rows`)"},
})
def export_referrals(body: AdminReferralExportRequest, request: Request,
                     user: AdminUser = Depends(require_permission("admin.referrals.export"))) -> StreamingResponse:
    """Super admin only. Same filters as the list, as a JSON body. Builds the file synchronously,
    audits the export (EXPORT, actor, filters, row count) and returns it as an attachment."""
    export = service.export(body, audit_context(user, request))
    return StreamingResponse(
        iter([export.content]),
        media_type=export.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.file_name}"',
            "Content-Length": str(len(export.content)),
            "X-Export-Row-Count": str(export.row_count),
        },
    )
