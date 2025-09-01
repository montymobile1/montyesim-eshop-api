from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config.context import currency_context


class CurrencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        currency = request.headers.get("x-currency", "USD").upper()
        token = currency_context.set(currency)

        try:
            response = await call_next(request)
            return response
        finally:
            # Reset the context
            currency_context.reset(token)
