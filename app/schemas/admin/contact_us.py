"""Shapes of GET /api/v1/admin/contact-us (MS-5081).

The list takes ``search`` (matched literally against the email) plus paging; no request body.
Rows are ``{id, email, content, created_at}`` (see ``app/repo/admin/contact_us_repo.py``).
"""
