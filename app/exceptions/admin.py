"""Errors raised by the admin API.

Admin code never raises ``HTTPException``: ``main.py`` rewrites 403 to 401 for those. These are
``CustomException`` subclasses, so the existing handler returns ``code`` as the HTTP status and
as ``responseCode``, the translated ``name`` as ``title``/``message``, and ``details`` as
``developerMessage``. ``details`` always carries the machine-readable code the portal reads.
"""
import json
from typing import Any

from app.config.constants import ErrorMessages
from app.exceptions import CustomException


class AdminError(CustomException):
    status_code: int = 400
    default_name: ErrorMessages = ErrorMessages.REQUEST_FAILED

    def __init__(self, details: str, name: ErrorMessages | str | None = None):
        super().__init__(name=name or self.default_name, details=details, code=self.status_code)


class AdminUnauthorized(AdminError):
    status_code = 401
    default_name = ErrorMessages.ADMIN_UNAUTHORIZED


class AdminForbidden(AdminError):
    status_code = 403
    default_name = ErrorMessages.ADMIN_FORBIDDEN


class AdminNotFound(AdminError):
    status_code = 404
    default_name = ErrorMessages.ADMIN_NOT_FOUND


class AdminConflict(AdminError):
    """409. ``developerMessage`` is a JSON string: ``{"code": "<code>", ...extra}``."""
    status_code = 409
    default_name = ErrorMessages.ADMIN_CONFLICT

    def __init__(self, code: str, name: ErrorMessages | str | None = None, **extra: Any):
        self.conflict_code = code
        self.extra = extra
        super().__init__(details=json.dumps({"code": code, **extra}, default=str), name=name)


class AdminPayloadTooLarge(AdminError):
    status_code = 413
    default_name = ErrorMessages.ADMIN_PAYLOAD_TOO_LARGE


class AdminValidationError(AdminError):
    status_code = 422
    default_name = ErrorMessages.ADMIN_VALIDATION_FAILED


class AdminRateLimited(AdminError):
    status_code = 429
    default_name = ErrorMessages.ADMIN_TOO_MANY_ATTEMPTS


class AdminUpstreamError(AdminError):
    """502: the database, storage or Supabase Auth failed. Details stay generic; the cause is logged."""
    status_code = 502
    default_name = ErrorMessages.ADMIN_UPSTREAM_ERROR
