import os
from contextvars import ContextVar

from app.models.user import UserModel

language_context: ContextVar[str] = ContextVar("language_context", default="en")
currency_context: ContextVar[str] = ContextVar("currency_context", default=os.getenv("DEFAULT_CURRENCY", "USD"))
device_id_context: ContextVar[str | None] = ContextVar("device_id_context", default=None)
auth_user_context: ContextVar[UserModel | None] = ContextVar("auth_user_context", default=None)
