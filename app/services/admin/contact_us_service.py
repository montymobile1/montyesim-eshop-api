"""Contact us messages for the admin portal (MS-5081, TEOS-118). Read-only."""
from datetime import date
from typing import Optional

from app.repo.admin.contact_us_repo import ContactUsAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.services.admin.audit_query_service import day_range


class ContactUsAdminService:
    def __init__(self, messages: Optional[ContactUsAdminRepo] = None):
        self.messages = messages or ContactUsAdminRepo()

    def list(self, page: PageParams, *, search: Optional[str] = None, date_from: Optional[date] = None,
             date_to: Optional[date] = None) -> AdminPage:
        """422 when ``date_from`` is after ``date_to`` (checked before any query)."""
        start, end = day_range(date_from, date_to)
        result = self.messages.search(page, search=search, created_from=start, created_to=end)
        return AdminPage(data=result.rows, total_count=result.total)
