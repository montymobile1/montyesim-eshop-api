"""Shared request/response shapes of the admin API (contract: docs section 4 of the migration plan)."""
from typing import Annotated, Any, Dict, Generic, List, Optional, TypeVar, Union

from fastapi import Query
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StringConstraints,
                      model_validator)

from app.schemas.response import Response, ResponseHelper

T = TypeVar("T")

MIN_REASON_LENGTH = 3
MAX_REASON_LENGTH = 500
#: The reason of an audited admin action (ban, unban, bundle status): 3 to 500 characters once the
#: surrounding spaces are trimmed, so "ab" or "   " is a 422 (TEOS-148, TEOS-166).
AdminReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=MIN_REASON_LENGTH,
                                               max_length=MAX_REASON_LENGTH)]
#: A JSON number, never a string: "5" is a 422 instead of being converted (TEOS-211, TEOS-215).
#: Integers stay integers; use with ``Field(gt=...)`` / ``Field(ge=..., le=...)`` for the bounds.
StrictNumber = Union[StrictInt, StrictFloat]

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
#: The admin list SQL functions take the offset (or the page) as ``integer`` (TEOS-141).
MAX_SQL_INT = 2_147_483_647


#: OpenAPI ``content`` of an admin error: the standard ``Response`` envelope (``responseCode`` = the HTTP
#: status, the machine code in ``developerMessage``), always JSON, also on routes answering 204 or a file.
#: Referenced rather than passed as ``model``: FastAPI would file a model under the route's own media type
#: (the xlsx exports) (TEOS-163).
ERROR_ENVELOPE = {"application/json": {"schema": {"$ref": "#/components/schemas/Response"}}}


class PageParams(BaseModel):
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @model_validator(mode="after")
    def _fit_sql_int(self) -> "PageParams":
        """A page whose offset (or number) would not fit an SQL ``integer`` becomes the last page that fits
        (TEOS-141): that page is past every row as well, so the list answers exactly like any other page
        past the end (no rows, the real total and facets) instead of 502 on an integer overflow."""
        if self.page_size >= 1:
            self.page = min(self.page, MAX_SQL_INT // self.page_size + 1, MAX_SQL_INT)
        return self

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size

    @property
    def range_end(self) -> int:
        """Inclusive end index for PostgREST ``range(offset, range_end)``."""
        return self.offset + self.page_size - 1


def page_params(page: int = Query(1, ge=1),
                page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)) -> PageParams:
    """Dependency for list routes: 1-based ``page``, ``page_size`` up to 100 (101 -> 422)."""
    return PageParams(page=page, page_size=page_size)


class AdminPage(BaseModel, Generic[T]):
    """``data`` of every list response: rows, the total row count, optional tab counts and totals."""
    data: List[T]
    total_count: int
    facets: Optional[Dict[str, int]] = None
    totals: Optional[Dict[str, Any]] = None


def ok(data: Any) -> Response:
    """200 with ``data`` in the standard envelope."""
    return ResponseHelper.success_data_response(data, 0)


def ok_page(page: AdminPage) -> Response:
    """200 list envelope; top-level ``totalCount`` mirrors ``data.total_count``."""
    return ResponseHelper.success_data_response(page, page.total_count)


class LookupItem(BaseModel):
    """One option of a ``GET /<resource>/lookup`` dropdown; resources may add extra fields."""
    id: str
    label: str

    model_config = ConfigDict(extra="allow")


class AdminStatusPatch(BaseModel):
    """Body of ``PATCH /<resource>/{id}/status``. ``is_active`` must be a JSON boolean: "false", "no",
    0 or 1 are refused with 422 instead of being converted (TEOS-175)."""
    is_active: StrictBool
    reason: Optional[str] = Field(None, min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")
