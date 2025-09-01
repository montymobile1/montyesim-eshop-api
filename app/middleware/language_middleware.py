from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config.context import language_context


class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        accept_language = request.headers.get("accept-language", "en")
        language = accept_language.split(",")[0].strip().lower()
        language = language if language in ['en', 'ar'] else 'en'

        token = language_context.set(language)

        try:
            response = await call_next(request)
            return response
        finally:
            # Reset the context
            language_context.reset(token)
