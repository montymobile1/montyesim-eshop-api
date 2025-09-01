from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config.context import device_id_context


class DeviceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        device_id = request.headers.get("x-device-id", None)
        token = device_id_context.set(device_id)

        try:
            response = await call_next(request)
            return response
        finally:
            device_id_context.reset(token)
