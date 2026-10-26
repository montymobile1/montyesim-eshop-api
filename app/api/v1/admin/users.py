"""Admin API: users. Owned by MS-5077 (lookup) and MS-5079 (users page and user detail)."""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import Depends, Path, Query, Request

from app.api.v1.admin._lookup import add_lookup_route
from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, require_permission
from app.schemas.admin.common import AdminPage, PageParams, ok, ok_page, page_params
from app.schemas.admin.users import (AdminUserBan, AdminUserBanResult, AdminUserDetail, AdminUserReferral,
                                     AdminUserRow, AdminUserUnban, AdminUserUnbanResult, AdminWalletTransaction,
                                     SortDirection, UserBanStatus, UserSort)
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.user_service import UserAdminService

router = admin_router()

READ = "admin.users.read"

# MS-5077: GET /lookup. Keep it above any /{id} route. A 1-character search is a 422 (TEOS-147).
add_lookup_route(router, "users", READ, min_search_length=2)

# ---------- MS-5079: users page and user detail ----------

service = UserAdminService()

UserId = Path(..., description="user id (uuid)")
SearchParam = Query(None, max_length=100, description="part of the email, matched literally")


@router.get("", response_model=Response[AdminPage[AdminUserRow]])
def list_users(search: Optional[str] = SearchParam,
               ban_status: UserBanStatus = Query("all"),
               sort: UserSort = Query("created_at"),
               direction: SortDirection = Query("desc", alias="dir"),
               page: PageParams = Depends(page_params),
               user: AdminUser = Depends(require_permission(READ))) -> Response:
    """Users with their ban state, newest first by default (`sort=created_at|email`, `dir=asc|desc`,
    ties broken by id), and `facets:{all,banned,active}` over the searched set."""
    return ok_page(service.list(search, ban_status, page, sort=sort, direction=direction))


@router.get("/{user_id}", response_model=Response[AdminUserDetail])
def get_user(user_id: UUID = UserId,
             user: AdminUser = Depends(require_permission(READ))) -> Response:
    """One user with `wallet:{amount,currency}` (null without a wallet). 404 unknown id."""
    return ok(service.get(str(user_id)))


@router.post("/{user_id}/ban", response_model=Response[AdminUserBanResult])
def ban_user(body: AdminUserBan, request: Request, user_id: UUID = UserId,
             user: AdminUser = Depends(require_permission("admin.users.ban"))) -> Response:
    """Bans for `hours` (default 8760) with a reason, audited server-side, and signs the user out
    everywhere. 404; 409 `already_banned`; 409 `cannot_ban_self`; 502 when Supabase Auth fails or
    when the audit row cannot be written (the ban is lifted again); 502 `session_revoke_failed` when
    the ban is applied and audited but the sessions could not be revoked (the ban stays)."""
    return ok(service.ban(str(user_id), body, audit_context(user, request, body.reason)))


@router.post("/{user_id}/unban", response_model=Response[AdminUserUnbanResult])
def unban_user(body: AdminUserUnban, request: Request, user_id: UUID = UserId,
               user: AdminUser = Depends(require_permission("admin.users.ban"))) -> Response:
    """Lifts the ban with a reason, audited with `previous_banned_until`. 404; 409 `not_banned`;
    409 `cannot_ban_self` on the caller's own id."""
    return ok(service.unban(str(user_id), body, audit_context(user, request, body.reason)))


@router.get("/{user_id}/devices", summary="The user's devices, newest first (404 unknown user)")
def list_user_devices(user_id: UUID = UserId, page: PageParams = Depends(page_params),
                      user: AdminUser = Depends(require_permission("admin.devices.read"))) -> Response:
    return ok_page(service.list_devices(str(user_id), page))


@router.get("/{user_id}/wallet-transactions", response_model=Response[AdminPage[AdminWalletTransaction]])
def list_user_wallet_transactions(user_id: UUID = UserId, page: PageParams = Depends(page_params),
                                  user: AdminUser = Depends(require_permission(READ))) -> Response:
    """The user's wallet transactions, newest first, with the true total. 404 unknown user."""
    return ok_page(service.list_wallet_transactions(str(user_id), page))


@router.get("/{user_id}/referrals", response_model=Response[AdminPage[AdminUserReferral]])
def list_user_referrals(user_id: UUID = UserId,
                        search: Optional[str] = Query(None, max_length=100, description="part of the referral code"),
                        from_email: Optional[str] = Query(None, max_length=100),
                        referral_date: Optional[date] = Query(None, alias="date"),
                        page: PageParams = Depends(page_params),
                        user: AdminUser = Depends(require_permission("admin.referrals.read"))) -> Response:
    """The user's referral transactions (same filters as `GET /referrals`). 404 unknown user."""
    return ok_page(service.list_referrals(str(user_id), page, search=search, from_email=from_email,
                                          referral_date=referral_date))
