"""Vouchers page of the admin portal (MS-5089, TEOS-133).

- list: search by code, status filter (all / active / inactive / used / expired), the five status tab
  counts, the user who used each voucher joined server-side from users_copy (replaces the browser-side
  users_copy merge);
- create: uniqueness by the database (unique lower(code)) instead of a browser pre-check (MS-5070); the
  code is optional: when omitted the server generates a 12-character code with ``secrets`` and retries on
  the unique-constraint conflict; the amount is capped by the VOUCHER_MAX_AMOUNT setting (TEOS-133);
- status and delete: audited in the same transaction as the change (MS-5071); a used voucher cannot be
  deleted (409 voucher_used) nor re-activated (409 already_used); deactivating it stays allowed.
"""
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional

from loguru import logger

from app.exceptions.admin import AdminConflict, AdminValidationError
from app.repo.admin.app_config_repo import AppConfigAdminRepo
from app.repo.admin.base import AuditContext
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.repo.admin.voucher_repo import VoucherAdminRepo
from app.schemas.admin.common import AdminPage, PageParams, AdminStatusPatch
from app.schemas.admin.settings import VOUCHER_MAX_AMOUNT_KEY
from app.schemas.admin.vouchers import (GENERATED_CODE_ALPHABET, GENERATED_CODE_LENGTH, MAX_VOUCHER_AMOUNT,
                                        AdminVoucherCreate)

#: Attempts to insert a server-generated code before giving up (each conflict draws a new code). With
#: 31^12 possible codes a single conflict is already extremely unlikely.
GENERATE_ATTEMPTS = 5


def generate_code() -> str:
    """12 characters from :data:`GENERATED_CODE_ALPHABET` (no 0/O, 1/I/L), drawn with a CSPRNG."""
    return "".join(secrets.choice(GENERATED_CODE_ALPHABET) for _ in range(GENERATED_CODE_LENGTH))


def parse_max_amount(value: Any) -> float:
    """The VOUCHER_MAX_AMOUNT setting as a number; the default (1 000 000) when missing or not > 0."""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return float(MAX_VOUCHER_AMOUNT)
    if value is None or not number.is_finite() or number <= 0:
        return float(MAX_VOUCHER_AMOUNT)
    return float(number)


def _is_expired(row: Dict[str, Any]) -> bool:
    if row.get("is_used") or not row.get("expired_at"):
        return False
    try:
        expires = datetime.fromisoformat(str(row["expired_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


class VoucherAdminService:
    def __init__(self, repo: Optional[VoucherAdminRepo] = None, users: Optional[UsersCopyAdminRepo] = None,
                 settings: Optional[AppConfigAdminRepo] = None):
        self._repo = repo
        self._users = users
        self._settings = settings

    # created lazily so importing the router never opens a database client
    @property
    def repo(self) -> VoucherAdminRepo:
        if self._repo is None:
            self._repo = VoucherAdminRepo()
        return self._repo

    @property
    def users(self) -> UsersCopyAdminRepo:
        if self._users is None:
            self._users = UsersCopyAdminRepo()
        return self._users

    @property
    def settings(self) -> AppConfigAdminRepo:
        if self._settings is None:
            self._settings = AppConfigAdminRepo()
        return self._settings

    def list(self, search: Optional[str], status: str, page: PageParams) -> AdminPage:
        result = self.repo.search(search, status, page)
        users = self._users_of(result.rows)
        currency = self._currency()
        return AdminPage(data=[self._shape(row, users, currency) for row in result.rows], total_count=result.total,
                         facets=self.repo.facets(search))

    def max_amount(self) -> float:
        """VOUCHER_MAX_AMOUNT (seeded in supabase_ddl.sql); 1 000 000 when the row is missing or invalid."""
        try:
            return parse_max_amount(self.settings.value_of(VOUCHER_MAX_AMOUNT_KEY))
        except Exception as exc:  # the cap must never block on a settings read error: fall back
            logger.warning(f"vouchers: {VOUCHER_MAX_AMOUNT_KEY} could not be read ({type(exc).__name__})")
            return float(MAX_VOUCHER_AMOUNT)

    def create(self, body: AdminVoucherCreate, ctx: AuditContext) -> Dict[str, Any]:
        """409 duplicate_code when an explicit code exists in any letter case (unique index); 422 when the
        amount is above VOUCHER_MAX_AMOUNT. Without a code, a generated code is retried on a conflict."""
        limit = self.max_amount()
        if body.amount > limit:
            raise AdminValidationError(f"amount: must be at most {limit:g} (VOUCHER_MAX_AMOUNT)")
        row = {"amount": body.amount, "expired_at": body.expired_at.isoformat() if body.expired_at else None}
        if body.code is not None:
            return self._shape(self.repo.insert_audited({"code": body.code, **row}, ctx), {}, self._currency())
        for attempt in range(1, GENERATE_ATTEMPTS + 1):
            try:
                created = self.repo.insert_audited({"code": generate_code(), **row}, ctx)
            except AdminConflict as exc:
                if exc.conflict_code != "duplicate_code" or attempt == GENERATE_ATTEMPTS:
                    raise
                logger.info(f"vouchers: generated code collided, retrying ({attempt}/{GENERATE_ATTEMPTS})")
                continue
            return self._shape(created, {}, self._currency())
        raise AdminConflict("duplicate_code")  # pragma: no cover (the loop returns or raises)

    def set_status(self, voucher_id: int, body: AdminStatusPatch, ctx: AuditContext) -> Dict[str, Any]:
        """404 when missing; 409 no_change when the voucher already has that state (from the SQL function);
        409 already_used when activating a voucher that was redeemed (checked under the row lock)."""
        if body.is_active:
            try:
                row = self.repo.update_audited(voucher_id, {"is_active": True}, ctx, guard={"is_used": False})
            except AdminConflict as exc:
                if exc.conflict_code == "guard_failed":
                    raise AdminConflict("already_used") from exc
                raise
        else:
            row = self.repo.update_audited(voucher_id, {"is_active": False}, ctx)
        return self._shape(row, self._users_of([row]), self._currency())

    def delete(self, voucher_id: int, ctx: AuditContext) -> None:
        """404 when missing; 409 voucher_used when the voucher was redeemed."""
        try:
            self.repo.delete_audited(voucher_id, ctx, guard={"is_used": False})
        except AdminConflict as exc:
            if exc.conflict_code == "guard_failed":
                raise AdminConflict("voucher_used") from exc
            raise

    # ---------- helpers ----------

    def _users_of(self, rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        ids = [row.get("used_by") for row in rows if row.get("used_by")]
        return self.users.by_ids(ids) if ids else {}

    def _currency(self) -> Optional[str]:
        """The default currency: the DEFAULT_CURRENCY setting, else the environment (as the public API)."""
        try:
            value = self.settings.value_of("DEFAULT_CURRENCY")
        except Exception:  # display only: never fail a voucher call on it
            value = None
        return (str(value).strip() if value and str(value).strip() else None) or os.getenv("DEFAULT_CURRENCY")

    @staticmethod
    def _shape(row: Dict[str, Any], users: Dict[str, Dict[str, Any]], currency: Optional[str] = None
               ) -> Dict[str, Any]:
        user = users.get(str(row.get("used_by"))) if row.get("used_by") else None
        used_by = {"id": user["id"], "email": user.get("email")} if user else None
        return {
            "id": row.get("id"),
            "code": row.get("code"),
            "amount": row.get("amount"),
            "currency": currency,
            "is_used": bool(row.get("is_used")),
            "is_active": bool(row.get("is_active", True)),
            "is_expired": _is_expired(row),
            "expired_at": row.get("expired_at"),
            "created_at": row.get("created_at"),
            "used_by": used_by,
            "used_by_user": used_by,
        }
