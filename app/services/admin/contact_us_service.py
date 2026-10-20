"""Contact us messages for the admin portal (MS-5081)."""
from typing import Optional

from app.repo.admin.contact_us_repo import ContactUsAdminRepo
from app.schemas.admin.common import AdminPage, PageParams


class ContactUsAdminService:
    def __init__(self, messages: Optional[ContactUsAdminRepo] = None):
        self.messages = messages or ContactUsAdminRepo()

    def list(self, page: PageParams, *, search: Optional[str] = None) -> AdminPage:
        result = self.messages.search(page, search=search)
        return AdminPage(data=result.rows, total_count=result.total)
