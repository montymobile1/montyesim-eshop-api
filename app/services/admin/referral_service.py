"""Referrals page of the admin portal (MS-5056).

- list: one page of referral transactions with the referrer and the referred user joined
  server-side (replaces the browser call to get_referral_transactions_with_count);
- export: the same rows as an .xlsx built on the server (replaces export_referrals_data, which
  sent every row to the browser as JSON and built the spreadsheet there). v1 is synchronous: the
  rows are read in chunks of 1000, the workbook is written with openpyxl in write-only mode,
  cells that could be read as formulas are neutralised, the export is capped by
  ADMIN_EXPORT_ROW_CAP (422 too_many_rows above it) and audited as EXPORT (actor, filters,
  row count) before the file is returned. The background job of the Jira story is a follow-up.
"""
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.exceptions.admin import AdminValidationError
from app.repo.admin.base import AuditContext
from app.repo.admin.referral_repo import MAX_RPC_LIMIT, ReferralAdminRepo
from app.schemas.admin.common import AdminPage, PageParams
from app.schemas.admin.referrals import AdminReferralFilters
from app.services.admin.audit_service import AuditService

#: Largest synchronous export; ``ADMIN_EXPORT_ROW_CAP`` in the environment overrides it.
DEFAULT_EXPORT_ROW_CAP = 50_000
#: Rows read per database call while exporting.
EXPORT_CHUNK_SIZE = MAX_RPC_LIMIT
#: Table name written in the EXPORT audit row.
AUDIT_TABLE = "user_order"
AUDIT_OPERATION = "EXPORT"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_TITLE = "Referral Transactions"

#: Column headers of the export, in order, and the row key each one reads.
EXPORT_COLUMNS = (
    ("Refer Code", "referral_code"),
    ("Reward Amount", "amount"),
    ("Currency", "currency"),
    ("Modified Amount", "modified_amount"),
    ("From Email", "referred_email"),
    ("To Email", "referrer_email"),
    ("Order Number", "id"),
    ("Order Status", "order_status"),
    ("Usage Status", "status"),
    ("Referral Date", "created_at"),
    ("Purchased Date", "payment_time"),
)

#: A text cell starting with one of these would be run as a formula by spreadsheet programs.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def export_row_cap() -> int:
    """``ADMIN_EXPORT_ROW_CAP`` (default 50000); an unreadable value falls back to the default."""
    try:
        return max(1, int(os.getenv("ADMIN_EXPORT_ROW_CAP", DEFAULT_EXPORT_ROW_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_EXPORT_ROW_CAP


def neutralise(value: Any) -> Any:
    """Numbers and empty values pass through; a text starting with = + - @ (or a tab / CR) gets a
    leading apostrophe so Excel, LibreOffice and Sheets show it as text instead of running it."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def export_file_name(now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"referrals-{stamp}.xlsx"


@dataclass(frozen=True)
class ReferralExport:
    content: bytes
    row_count: int
    file_name: str
    media_type: str = XLSX_MEDIA_TYPE


class ReferralAdminService:
    def __init__(self, repo: Optional[ReferralAdminRepo] = None, audit: Optional[AuditService] = None):
        self._repo = repo
        self._audit = audit

    # created lazily so importing the router never opens a database client
    @property
    def repo(self) -> ReferralAdminRepo:
        if self._repo is None:
            self._repo = ReferralAdminRepo()
        return self._repo

    @property
    def audit(self) -> AuditService:
        if self._audit is None:
            self._audit = AuditService()
        return self._audit

    def list(self, filters: AdminReferralFilters, page: PageParams) -> AdminPage:
        result = self.repo.search(filters, page.limit, page.offset)
        return AdminPage(data=[self._shape(row) for row in result.rows], total_count=result.total)

    def export(self, filters: AdminReferralFilters, ctx: AuditContext) -> ReferralExport:
        """The matching rows as an .xlsx. 422 ``too_many_rows`` (with ``total`` and ``cap``) when the
        filters match more than ``ADMIN_EXPORT_ROW_CAP`` rows; nothing is read beyond the first chunk
        and nothing is audited in that case. The audit row is written before the file is returned, so
        an audit failure (502) means no file left the server."""
        cap = export_row_cap()
        first = self.repo.search(filters, EXPORT_CHUNK_SIZE, 0)
        if first.total > cap:
            raise AdminValidationError(json.dumps({"code": "too_many_rows", "total": first.total, "cap": cap}))
        rows = list(first.rows)
        offset = len(first.rows)
        while offset < first.total and len(first.rows) == EXPORT_CHUNK_SIZE:
            chunk = self.repo.search(filters, EXPORT_CHUNK_SIZE, offset)
            if not chunk.rows:
                break
            rows.extend(chunk.rows)
            offset += len(chunk.rows)
            if len(rows) > cap:
                raise AdminValidationError(json.dumps({"code": "too_many_rows", "total": len(rows), "cap": cap}))
        content = self._workbook(self._shape(row) for row in rows)
        file_name = export_file_name()
        self.audit.log(ctx, AUDIT_TABLE, AUDIT_OPERATION, rows_affected=len(rows),
                       filters=filters.audit_filters(), file_name=file_name)
        return ReferralExport(content=content, row_count=len(rows), file_name=file_name)

    # ---------- helpers ----------

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        referrer = row.get("referrer") if isinstance(row.get("referrer"), dict) else None
        referred = row.get("referred") if isinstance(row.get("referred"), dict) else None
        return {
            "id": _text(row.get("id")),
            "created_at": _text(row.get("created_at")),
            "payment_time": _text(row.get("payment_time")),
            "referral_code": row.get("referral_code"),
            "amount": row.get("amount"),
            "currency": row.get("currency"),
            "modified_amount": row.get("modified_amount"),
            "order_status": row.get("order_status"),
            "status": row.get("status"),
            "referrer": {"id": _text(referrer.get("id")), "email": referrer.get("email")} if referrer else None,
            "referred": {"id": _text(referred.get("id")), "email": referred.get("email")} if referred else None,
        }

    @staticmethod
    def _workbook(rows: Iterable[Dict[str, Any]]) -> bytes:
        """An .xlsx with one sheet: the header row, then one row per transaction (write-only mode
        keeps memory flat whatever the row count)."""
        from openpyxl import Workbook  # imported here so the module loads without openpyxl installed

        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet(SHEET_TITLE)
        sheet.append([header for header, _ in EXPORT_COLUMNS])
        for row in rows:
            flat = {**row,
                    "referrer_email": (row.get("referrer") or {}).get("email"),
                    "referred_email": (row.get("referred") or {}).get("email")}
            sheet.append([neutralise(flat.get(key)) for _, key in EXPORT_COLUMNS])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)
