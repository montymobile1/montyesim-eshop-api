"""Base class of every admin repository.

It extends the shared ``BaseRepository`` without changing it and adds what every admin list and
write needs:

- ``q``: a select that also returns the total row count;
- ``like`` / ``or_ilike`` / ``filters``: search and filters built from user input safely (the
  text is always matched literally; ``%``, ``_``, commas, quotes and parentheses cannot change
  the query);
- ``paged``: a stable order (``created_at desc, id desc`` by default) plus the page and the total;
- ``insert_audited`` / ``update_audited`` / ``delete_audited``: writes through the
  ``admin_*_audited`` SQL functions, which write the audit row in the same transaction;
- ``_run``: database errors turned into admin errors (409 duplicate / referenced, 404, 422, 502).
"""
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Type

from loguru import logger
from postgrest.exceptions import APIError
from postgrest.types import CountMethod
from pydantic import BaseModel, ConfigDict

from app.config.db import DatabaseTables
from app.exceptions.admin import AdminConflict, AdminNotFound, AdminUpstreamError, AdminValidationError
from app.repo.base_repo import BaseRepository
from app.schemas.admin.common import PageParams

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")
DEFAULT_ORDER: Tuple[Tuple[str, bool], ...] = (("created_at", True), ("id", True))


class AdminRow(BaseModel):
    """Placeholder model for BaseRepository; admin repositories work with plain dicts."""
    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True)
class Paged:
    rows: List[Dict[str, Any]]
    total: int


@dataclass(frozen=True)
class AuditContext:
    """Who did a write and why; passed to every audited write."""
    actor_id: str
    ip: Optional[str] = None
    reason: Optional[str] = None


def _column(name: str) -> str:
    """Column names come from code, never from the request; this guards against mistakes."""
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ValueError(f"invalid column name: {name!r}")
    return name


class AdminBaseRepository(BaseRepository):
    #: Unique-constraint name fragment -> 409 code returned to the portal, e.g.
    #: {"promotion_code_key": "duplicate_code"}. Unlisted duplicates return "duplicate".
    UNIQUE_CONFLICTS: Dict[str, str] = {}

    def __init__(self, table_name: DatabaseTables, model: Type[BaseModel] = AdminRow):
        super().__init__(table_name, model)
        self.table_name = str(table_name)

    # ---------- building queries ----------

    def q(self, select: str = "*", *, count: bool = True, head: bool = False):
        """A select on this table; with ``count`` the response also carries the total row count."""
        return self.table.select(select, count=CountMethod.exact if count else None, head=head or None)

    @staticmethod
    def like(term: str) -> str:
        """``%term%`` with ``\\``, ``%`` and ``_`` escaped, so the term is matched literally."""
        escaped = str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def quoted(value: Any) -> str:
        """A value inside a PostgREST ``or=(...)`` / ``in.(...)`` list, double-quoted and escaped,
        so commas, dots, quotes and parentheses in user input stay part of the value."""
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{text}"'

    def or_ilike(self, query, columns: Sequence[str], term: Optional[str]):
        """Rows where any of ``columns`` contains ``term`` (case-insensitive). No-op for a blank term."""
        if term is None or not str(term).strip():
            return query
        pattern = self.quoted(self.like(str(term).strip()))
        return query.or_(",".join(f"{_column(c)}.ilike.{pattern}" for c in columns))

    def filters(self, query, *, eq: Optional[Dict[str, Any]] = None, neq: Optional[Dict[str, Any]] = None,
                ilike: Optional[Dict[str, Optional[str]]] = None, in_: Optional[Dict[str, Iterable[Any]]] = None,
                not_in: Optional[Dict[str, Iterable[Any]]] = None, gte: Optional[Dict[str, Any]] = None,
                lte: Optional[Dict[str, Any]] = None, is_null: Optional[Dict[str, bool]] = None):
        """Applies the given filters; ``None`` values and empty ``ilike`` terms are skipped.
        An empty ``in_`` list matches nothing; an empty ``not_in`` list filters nothing."""
        for column, value in (eq or {}).items():
            if value is not None:
                query = query.eq(_column(column), value)
        for column, value in (neq or {}).items():
            if value is not None:
                query = query.neq(_column(column), value)
        for column, term in (ilike or {}).items():
            if term is not None and str(term).strip():
                query = query.ilike(_column(column), self.like(str(term).strip()))
        for column, values in (in_ or {}).items():
            if values is None:
                continue
            # an empty list becomes in.() which PostgREST answers with no rows
            query = query.filter(_column(column), "in", f"({','.join(self.quoted(v) for v in values)})")
        for column, values in (not_in or {}).items():
            values = list(values or [])
            if values:
                query = query.not_.filter(_column(column), "in", f"({','.join(self.quoted(v) for v in values)})")
        for column, value in (gte or {}).items():
            if value is not None:
                query = query.gte(_column(column), value)
        for column, value in (lte or {}).items():
            if value is not None:
                query = query.lte(_column(column), value)
        for column, flag in (is_null or {}).items():
            if flag is True:
                query = query.is_(_column(column), "null")
            elif flag is False:
                query = query.not_.is_(_column(column), "null")
        return query

    # ---------- running queries ----------

    def paged(self, query, page: PageParams, order: Sequence[Tuple[str, bool]] = DEFAULT_ORDER) -> Paged:
        """One page of ``query`` in a stable order, with the total row count (``q(count=True)``)."""
        for column, desc in order:
            query = query.order(_column(column), desc=desc)
        response = self._run(lambda: query.range(page.offset, page.range_end).execute())
        rows = response.data or []
        total = response.count if response.count is not None else len(rows)
        return Paged(rows=rows, total=total)

    def count(self, **filter_kwargs) -> int:
        """Number of rows matching ``filters(**filter_kwargs)``; no rows are transferred."""
        query = self.filters(self.q("id", count=True, head=True), **filter_kwargs)
        response = self._run(lambda: query.execute())
        return response.count or 0

    def one(self, record_id: Any, select: str = "*") -> Optional[Dict[str, Any]]:
        response = self._run(lambda: self.table.select(select).eq("id", record_id).limit(1).execute())
        return response.data[0] if response.data else None

    def get_or_404(self, record_id: Any, select: str = "*") -> Dict[str, Any]:
        row = self.one(record_id, select)
        if row is None:
            raise AdminNotFound("not_found")
        return row

    def rpc(self, function: str, params: Dict[str, Any]) -> Any:
        """Calls a database function and returns its raw result."""
        return self._run(lambda: self.client.rpc(_column(function), params).execute()).data

    # ---------- audited writes (SQL: migrations/admin/050_admin_audit_helpers.sql) ----------

    def insert_audited(self, row: Dict[str, Any], ctx: AuditContext, rotate_cache: bool = False) -> Dict[str, Any]:
        return self.rpc("admin_insert_audited", {
            "p_table": self.table_name, "p_row": row, "p_actor": ctx.actor_id, "p_ip": ctx.ip,
            "p_reason": ctx.reason, "p_rotate_cache": rotate_cache,
        })

    def update_audited(self, record_id: Any, patch: Dict[str, Any], ctx: AuditContext, *,
                       operation: str = "UPDATE", rotate_cache: bool = False,
                       guard: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Updates one row. ``guard`` = column values the row must still have (else 409 guard_failed);
        an update that changes nothing returns 409 no_change."""
        return self.rpc("admin_update_audited", {
            "p_table": self.table_name, "p_id": str(record_id), "p_patch": patch, "p_actor": ctx.actor_id,
            "p_ip": ctx.ip, "p_reason": ctx.reason, "p_operation": operation,
            "p_rotate_cache": rotate_cache, "p_guard": guard,
        })

    def delete_audited(self, record_id: Any, ctx: AuditContext, *, rotate_cache: bool = False,
                       guard: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Deletes one row and returns it as it was."""
        return self.rpc("admin_delete_audited", {
            "p_table": self.table_name, "p_id": str(record_id), "p_actor": ctx.actor_id, "p_ip": ctx.ip,
            "p_reason": ctx.reason, "p_rotate_cache": rotate_cache, "p_guard": guard,
        })

    # ---------- error mapping ----------

    def _run(self, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except APIError as exc:
            raise self._map_api_error(exc) from exc
        except (AdminConflict, AdminNotFound, AdminValidationError, AdminUpstreamError):
            raise
        except Exception as exc:  # network, timeout, unexpected client error
            logger.error(f"admin repository {self.table_name}: {type(exc).__name__}: {exc}")
            raise AdminUpstreamError("DATABASE_ERROR") from exc

    def _map_api_error(self, exc: APIError) -> Exception:
        code = exc.code or ""
        message = exc.message or ""
        detail = _detail_json(exc.details)
        if code == "23505":
            text = f"{message} {exc.details or ''}"
            for fragment, conflict_code in self.UNIQUE_CONFLICTS.items():
                if fragment in text:
                    return AdminConflict(conflict_code)
            return AdminConflict("duplicate")
        if code == "23503":
            return AdminConflict("referenced")
        if code == "AD404":
            return AdminNotFound(message or "not_found")
        if code == "AD409":
            conflict_code = detail.pop("code", None) or message or "conflict"
            return AdminConflict(conflict_code, **detail)
        if code == "AD422" or code in ("22P02", "23502", "23514", "P0001"):
            return AdminValidationError(message or "invalid input")
        logger.error(f"admin repository {self.table_name}: database error {code}: {message}")
        return AdminUpstreamError("DATABASE_ERROR")


def _detail_json(details: Optional[str]) -> Dict[str, Any]:
    if not details:
        return {}
    try:
        value = json.loads(details)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
