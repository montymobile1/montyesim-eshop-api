"""contact_us for the admin Contact us page (MS-5081, TEOS-118)."""
from datetime import datetime
from typing import Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

COLUMNS = "id,email,content,created_at"
SEARCH_COLUMNS = ("email", "content")


class ContactUsAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_CONTACT_US)

    def search(self, page: PageParams, *, search: Optional[str] = None,
               created_from: Optional[datetime] = None, created_to: Optional[datetime] = None) -> Paged:
        """One page of messages, newest first (``created_at desc, id desc``). ``search`` matches the email
        or the content literally; ``created_from`` is inclusive and ``created_to`` exclusive."""
        query = self.or_ilike(self.q(COLUMNS), SEARCH_COLUMNS, search)
        if created_from is not None:
            query = query.gte("created_at", created_from.isoformat())
        if created_to is not None:
            query = query.lt("created_at", created_to.isoformat())
        return self.paged(query, page)
