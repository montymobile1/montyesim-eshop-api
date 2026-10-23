"""Admin portal authentication: /api/v1/admin/auth/*."""
from fastapi import Depends, Request
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, admin_user, rate_limit_ip
from app.schemas.admin.auth import AdminLoginRequest, AdminProfile, AdminRefreshRequest, AdminSession
from app.schemas.admin.common import ok
from app.schemas.response import Response
from app.services.admin.auth_service import AdminAuthService

router = admin_router()

service = AdminAuthService()


@router.post("/login", response_model=Response[AdminSession])
def login(body: AdminLoginRequest, request: Request) -> Response:
    """Email + password sign-in. 401 ADMIN_LOGIN_FAILED, 403 ADMIN_ROLE_REQUIRED, 429 too many attempts."""
    return ok(service.login(body.email, body.password, rate_limit_ip(request)))


@router.post("/refresh", response_model=Response[AdminSession])
def refresh(body: AdminRefreshRequest) -> Response:
    """New access token from a refresh token. The role is checked again."""
    return ok(service.refresh(body.refresh_token))


@router.post("/logout", status_code=204, response_class=HttpResponse)
def logout(user: AdminUser = Depends(admin_user)) -> HttpResponse:
    """Ends the caller's session."""
    service.logout(user)
    return HttpResponse(status_code=204)


@router.get("/me", response_model=Response[AdminProfile])
def me(user: AdminUser = Depends(admin_user)) -> Response:
    """The signed-in admin with current role and permissions (the portal re-reads this on load)."""
    return ok(service.me(user))
