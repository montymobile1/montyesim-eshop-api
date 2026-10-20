"""contact_us for the admin Contact us page (MS-5081)."""
from typing import Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

COLUMNS = "id,email,content,created_at"


class ContactUsAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_CONTACT_US)

    def search(self, page: PageParams, *, search: Optional[str] = None) -> Paged:
        """One page of messages, newest first; ``search`` matches the email literally."""
        query = self.or_ilike(self.q(COLUMNS), ["email"], search)
        return self.paged(query, page)
