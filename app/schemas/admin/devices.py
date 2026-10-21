"""Shapes of GET /api/v1/admin/devices (MS-5081).

The list takes ``user_id`` (a UUID, else 422) plus paging; no request body. Rows are ``device``
rows without ``fcm_token`` (see ``app/repo/admin/device_repo.py``).
"""
