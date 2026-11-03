"""Shapes of GET /api/v1/admin/contact-us (MS-5081, TEOS-118).

The list takes ``search`` (matched literally, case-insensitive, against the email OR the message content),
``from`` / ``to`` (inclusive UTC days) plus paging; no request body. Read-only: no write routes.
Rows are ``{id, email, content, created_at}`` (see ``app/repo/admin/contact_us_repo.py``); the
``contact_us`` table has no ``user_id`` column, so it is not returned.
"""
from typing import Optional

from pydantic import BaseModel

#: Longest accepted ``search`` term (TEOS-118); longer is 422.
SEARCH_MAX = 100


class AdminContactMessage(BaseModel):
    """A contact-us message as stored: the email is not normalised and the content is returned in full,
    as plain text (the portal never renders it as HTML)."""
    id: int
    email: Optional[str] = None
    content: Optional[str] = None
    created_at: Optional[str] = None
