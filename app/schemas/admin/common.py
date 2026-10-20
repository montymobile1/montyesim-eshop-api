"""Shared request/response shapes of the admin API (contract: docs section 4 of the migration plan)."""
from typing import Any, Dict, Generic, List, Optional, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.response import Response, ResponseHelper

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class PageParams(BaseModel):
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

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


class StatusPatch(BaseModel):
    """Body of ``PATCH /<resource>/{id}/status``."""
    is_active: bool
    reason: Optional[str] = Field(None, min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")
