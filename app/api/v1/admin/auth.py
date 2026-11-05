"""Admin portal authentication: /api/v1/admin/auth/*."""
from typing import Union

from fastapi import Depends, Request
from starlette.responses import Response as HttpResponse

from app.api.v1.admin._route import admin_router
from app.dependencies.admin_security import AdminUser, admin_user, rate_limit_ip
from app.schemas.admin.auth import (AdminChangePasswordRequest, AdminLoginRequest, AdminPasswordChanged,
                                    AdminPasswordReset, AdminProfile, AdminRefreshRequest,
                                    AdminResetPasswordRequest, AdminSession)
from app.schemas.admin.trusted_devices import (AdminCodeResent, AdminLoginChallenge, AdminResendCodeRequest,
                                               AdminVerifyDeviceRequest)
from app.schemas.admin.common import ok
from app.schemas.response import Response
from app.services.admin.audit_service import audit_context
from app.services.admin.auth_service import AdminAuthService

router = admin_router()

service = AdminAuthService()


@router.post("/login", response_model=Response[Union[AdminSession, AdminLoginChallenge]])
def login(body: AdminLoginRequest, request: Request) -> Response:
    """Email + password sign-in. 401 ADMIN_LOGIN_FAILED, 403 ADMIN_ROLE_REQUIRED, 429 too many attempts.
    TEOS-255: a trusted device gets the session (`status: signed_in`); any other browser gets
    `status: verification_required` and an email code (then `POST /verify-device`). 429 `code_limit`,
    503 `email_unavailable`."""
    return ok(service.login(body.email, body.password, rate_limit_ip(request), body.device,
                            request.headers.get("user-agent")))


@router.post("/verify-device", response_model=Response[AdminSession])
def verify_device(body: AdminVerifyDeviceRequest, request: Request) -> Response:
    """TEOS-255: the email code of a login challenge -> the session (+ `device_id`). 422 `code_format`;
    400 `code_invalid {attempts_left}` / `device_mismatch`; 429 `too_many_attempts`;
    410 `challenge_expired` / `challenge_closed`; 403 not an admin; 502 Supabase Auth down."""
    return ok(service.verify_device(body.challenge_id, body.code, body.trust_device, body.device_name,
                                    body.device, rate_limit_ip(request)))


@router.post("/resend-code", response_model=Response[AdminCodeResent])
def resend_code(body: AdminResendCodeRequest, request: Request) -> Response:
    """TEOS-255: a new email code for a login challenge (the previous one stops working). 410
    `challenge_closed` / `challenge_expired`; 429 `resend_limit`, `resend_too_soon {retry_after}`,
    `code_limit {retry_after}`; 503 `email_unavailable`."""
    return ok(service.resend_code(body.challenge_id, rate_limit_ip(request)))


@router.post("/refresh", response_model=Response[AdminSession])
def refresh(body: AdminRefreshRequest, request: Request) -> Response:
    """New access token from a refresh token. The role is checked again. TEOS-255: `device` must be the
    browser of the session's device row: else 401 `device_session_required`; 401 `device_revoked` when
    the device was revoked."""
    return ok(service.refresh(body.refresh_token, body.device, rate_limit_ip(request)))


@router.post("/logout", status_code=204, response_class=HttpResponse)
def logout(user: AdminUser = Depends(admin_user)) -> HttpResponse:
    """Ends the caller's session."""
    service.logout(user)
    return HttpResponse(status_code=204)


@router.get("/me", response_model=Response[AdminProfile])
def me(user: AdminUser = Depends(admin_user)) -> Response:
    """The signed-in admin with current role and permissions (the portal re-reads this on load)."""
    return ok(service.me(user))


@router.post("/change-password", response_model=Response[AdminPasswordChanged])
def change_password(body: AdminChangePasswordRequest, request: Request,
                    user: AdminUser = Depends(admin_user)) -> Response:
    """TEOS-307: the caller's own password. Returns a new session (replace both tokens): every other session
    of the account is signed out, the one used for this call included. 400 `current_password_invalid`;
    422 `same_password` / `weak_password` or a new password that breaks the rules; 429 like the login."""
    return ok(service.change_password(user, body.current_password, body.new_password, rate_limit_ip(request),
                                      audit_context(user, request)))


@router.post("/reset-password", response_model=Response[AdminPasswordReset])
def reset_password(body: AdminResetPasswordRequest, request: Request) -> Response:
    """TEOS-307: sets the new password from the reset email link (no bearer: `access_token` is the link's
    session). Every session of the account is then signed out. 400 `reset_link_invalid`; 403 not an
    admin; 422 `same_password` / `weak_password` or a new password that breaks the rules."""
    return ok(service.complete_password_reset(body.access_token, body.new_password, rate_limit_ip(request)))
