"""Admin API: contact_us. Owned by MS-5081."""
from typing import Optional

from fastapi import Depends, Query

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import PageParams, ok_page, page_params
from app.services.admin.contact_us_service import ContactUsAdminService

router = admin_router()
service = ContactUsAdminService()


@router.get("", summary="List contact-us messages, newest first")
def list_contact_messages(page: PageParams = Depends(page_params),
                          search: Optional[str] = Query(None, max_length=200),
                          user: AdminUser = Depends(require_permission("admin.contact_us.read"))):
    return ok_page(service.list(page, search=search))
