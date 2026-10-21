"""Route class shared by every admin endpoint.

It keeps the admin contract without editing ``main.py``:
- unknown query parameters -> 422 (a typo in a filter must not silently return everything);
- JSON bodies larger than ``ADMIN_MAX_JSON_BYTES`` -> 413;
- request validation errors -> 422 with HTTP status 422 (``main.py`` answers 400 for those);
- ``DatabaseException`` from the shared repositories -> 502 with a generic message.
"""
import os
from typing import Callable, Coroutine, Any, Set

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from loguru import logger
from starlette.responses import Response

from app.exceptions import DatabaseException
from app.exceptions.admin import AdminPayloadTooLarge, AdminUpstreamError, AdminValidationError

DEFAULT_MAX_JSON_BYTES = 256 * 1024


def _max_json_bytes() -> int:
    try:
        return int(os.getenv("ADMIN_MAX_JSON_BYTES", DEFAULT_MAX_JSON_BYTES))
    except ValueError:
        return DEFAULT_MAX_JSON_BYTES


def _declared_query_params(route: APIRoute) -> Set[str]:
    names: Set[str] = set()
    pending = [route.dependant]
    seen = set()
    while pending:
        dependant = pending.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        names.update(field.alias for field in dependant.query_params)
        pending.extend(dependant.dependencies)
    return names


def _format_validation(errors) -> str:
    parts = []
    for error in errors:
        location = ".".join(str(p) for p in error.get("loc", ()) if p != "body")
        parts.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    return "; ".join(parts) or "invalid request"


class AdminRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()
        allowed_query = _declared_query_params(self)

        async def admin_handler(request: Request) -> Response:
            unknown = sorted({key for key in request.query_params.keys() if key not in allowed_query})
            if unknown:
                raise AdminValidationError(f"unknown query parameter(s): {', '.join(unknown)}")
            if "application/json" in request.headers.get("content-type", ""):
                length = request.headers.get("content-length")
                if length and length.isdigit() and int(length) > _max_json_bytes():
                    raise AdminPayloadTooLarge("request body too large")
            try:
                return await original_handler(request)
            except RequestValidationError as exc:
                raise AdminValidationError(_format_validation(exc.errors()))
            except DatabaseException as exc:
                logger.error(f"admin database error on {request.method} {request.url.path}: {exc.details}")
                raise AdminUpstreamError("DATABASE_ERROR")

        return admin_handler


def admin_router(**kwargs) -> APIRouter:
    """APIRouter whose routes use AdminRoute. Every admin router file must be created with this."""
    return APIRouter(route_class=AdminRoute, **kwargs)
