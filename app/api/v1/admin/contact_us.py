"""Admin API: contact_us. Owned by MS-5081, TEOS-118. Read only: no route creates, edits or deletes messages."""
from datetime import date
from typing import Optional

from fastapi import Depends, Query

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import AdminPage, PageParams, ok_page, page_params
from app.schemas.admin.contact_us import SEARCH_MAX, AdminContactMessage
from app.schemas.response import Response
from app.services.admin.contact_us_service import ContactUsAdminService

router = admin_router()
service = ContactUsAdminService()


@router.get("", response_model=Response[AdminPage[AdminContactMessage]],
            summary="List contact-us messages, newest first")
def list_contact_messages(page: PageParams = Depends(page_params),
                          search: Optional[str] = Query(
                              None, max_length=SEARCH_MAX,
                              description="Matches the email or the message content (case-insensitive, literal)"),
                          date_from: Optional[date] = Query(None, alias="from", description="First day (UTC), inclusive"),
                          date_to: Optional[date] = Query(None, alias="to", description="Last day (UTC), inclusive"),
                          user: AdminUser = Depends(require_permission("admin.contact_us.read"))):
    """Fixed order ``created_at desc, id desc`` (no sort parameter). 422: invalid date or page, search over
    100 characters, ``from`` after ``to``, unknown parameter. 401 no session, 403 missing permission,
    502 database error."""
    return ok_page(service.list(page, search=search, date_from=date_from, date_to=date_to))
