"""Vouchers page of the admin portal (MS-5089).

- list: search by code, status filter, status tab counts, the user who used each voucher joined
  server-side from users_copy (replaces the browser-side users_copy merge);
- create: uniqueness by the database (unique lower(code)) instead of a browser pre-check (MS-5070);
- status and delete: audited in the same transaction as the change (MS-5071); a used voucher
  cannot be deleted (409 voucher_used).
"""
from typing import Any, Dict, Iterable, Optional

from app.exceptions.admin import AdminConflict
from app.repo.admin.base import AuditContext
from app.repo.admin.users_copy_repo import UsersCopyAdminRepo
from app.repo.admin.voucher_repo import VoucherAdminRepo
from app.schemas.admin.common import AdminPage, PageParams, AdminStatusPatch
from app.schemas.admin.vouchers import AdminVoucherCreate


class VoucherAdminService:
    def __init__(self, repo: Optional[VoucherAdminRepo] = None, users: Optional[UsersCopyAdminRepo] = None):
        self._repo = repo
        self._users = users

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

    def list(self, search: Optional[str], status: str, page: PageParams) -> AdminPage:
        result = self.repo.search(search, status, page)
        users = self._users_of(result.rows)
        return AdminPage(data=[self._shape(row, users) for row in result.rows], total_count=result.total,
                         facets=self.repo.facets(search))

    def create(self, body: AdminVoucherCreate, ctx: AuditContext) -> Dict[str, Any]:
        """409 duplicate_code when the code exists in any letter case (unique index)."""
        row = {"code": body.code, "amount": body.amount,
               "expired_at": body.expired_at.isoformat() if body.expired_at else None}
        return self._shape(self.repo.insert_audited(row, ctx), {})

    def set_status(self, voucher_id: int, body: AdminStatusPatch, ctx: AuditContext) -> Dict[str, Any]:
        """404 when missing; 409 no_change when the voucher already has that state (from the SQL function)."""
        row = self.repo.update_audited(voucher_id, {"is_active": body.is_active}, ctx)
        return self._shape(row, self._users_of([row]))

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

    @staticmethod
    def _shape(row: Dict[str, Any], users: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        user = users.get(str(row.get("used_by"))) if row.get("used_by") else None
        return {
            "id": row.get("id"),
            "code": row.get("code"),
            "amount": row.get("amount"),
            "is_used": bool(row.get("is_used")),
            "is_active": bool(row.get("is_active", True)),
            "expired_at": row.get("expired_at"),
            "created_at": row.get("created_at"),
            "used_by_user": {"id": user["id"], "email": user.get("email")} if user else None,
        }
