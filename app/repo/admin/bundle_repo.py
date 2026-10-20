"""bundle for the admin API.

- ``lookup``: MS-5077 (bundle dropdowns on promotions, promo analysis);
- list, detail, edit and status methods are added by MS-5083.
"""
from typing import Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository, Paged
from app.schemas.admin.common import PageParams

LOOKUP_SELECT = "id,display_title:data->>display_title,bundle_code:data->>bundle_code"


class BundleAdminRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_BUNDLE)

    def lookup(self, search: Optional[str], page: PageParams, record_id: Optional[str] = None) -> Paged:
        """``{id, display_title, bundle_code}`` rows; ``search`` matches ``data->>bundle_code``
        literally (as the portal's bundle dropdowns do). Ordered by display title, then id."""
        query = self.filters(self.q(LOOKUP_SELECT), eq={"id": record_id})
        if search is not None and search.strip():
            # JSON path: not a plain column, so it cannot go through filters()/or_ilike (column guard)
            query = query.ilike("data->>bundle_code", self.like(search.strip()))
        query = query.order("data->>display_title")
        return self.paged(query, page, order=(("id", False),))
