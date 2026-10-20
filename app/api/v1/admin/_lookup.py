"""``GET /<resource>/lookup`` for the portal's async dropdowns (MS-5077).

``add_lookup_route(router, "<LookupService method>", "<permission>")`` adds the route to a resource
router, so every lookup has the same parameters and response:
``?search&page&page_size`` -> ``{data: [{id, label, ...extra}], total_count}``.
``?id=<id>`` returns only that item (the portal uses it to show a dropdown's label after a reload).
Add it before any ``/{id}`` route of the same router, so ``lookup`` is not read as an id.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import PageParams, ok_page, page_params
from app.services.admin import lookup_service

MAX_SEARCH_LENGTH = 100


def add_lookup_route(router: APIRouter, method: str, permission: str) -> None:
    if not callable(getattr(lookup_service.LookupService, method, None)):
        raise ValueError(f"LookupService has no lookup {method!r}")

    def lookup(search: Optional[str] = Query(None, max_length=MAX_SEARCH_LENGTH),
               record_id: Optional[str] = Query(None, alias="id", min_length=1, max_length=100),
               page: PageParams = Depends(page_params),
               user: AdminUser = Depends(require_permission(permission))):
        # resolved per request, so tests can replace lookup_service.service
        return ok_page(getattr(lookup_service.service, method)(search, page, record_id))

    router.add_api_route("/lookup", lookup, methods=["GET"], name=f"{method}_lookup",
                         summary=f"Dropdown options: {method.replace('_', ' ')} ({{id, label}}), paged")
