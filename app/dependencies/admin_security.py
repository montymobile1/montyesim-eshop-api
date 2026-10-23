"""Authentication and authorization for the admin API.

``admin_user`` validates the bearer token with Supabase Auth on every request and reads the
role from ``app_metadata.role``, so revoking a role or banning an admin takes effect on the
next request instead of when the token expires. ``require_permission`` is the per-route check.
"""
import ipaddress
import os
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Optional

import jwt
from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger

from app.config.admin_permissions import permissions_for
from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.exceptions.admin import AdminForbidden, AdminUnauthorized

# auto_error=False: with True FastAPI raises HTTPException(403), which main.py rewrites to 401
# with a generic body. A missing header is reported by admin_user as AdminUnauthorized instead.
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AdminUser:
    id: str
    email: Optional[str]
    role: str
    permissions: FrozenSet[str] = field(default_factory=frozenset)
    token: str = field(default="", repr=False)

    def has(self, permission: str) -> bool:
        return permission in self.permissions


def _verify_signature(token: str) -> None:
    """Cheap local check before the network call; skipped when SUPABASE_JWT_SECRET is not set
    (projects on asymmetric signing keys). Supabase Auth's get_user stays the authority."""
    secret = os.getenv("SUPABASE_JWT_SECRET")
    if not secret:
        return
    jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")


def resolve_admin(token: str) -> AdminUser:
    """Validate ``token`` with Supabase Auth and return the admin it belongs to.

    Raises AdminUnauthorized (401) for a missing, invalid, expired or revoked session and
    AdminForbidden (403) when the account has no admin role.
    """
    if not token:
        raise AdminUnauthorized("BEARER_TOKEN_REQUIRED")
    try:
        _verify_signature(token)
        response = supabase_client().auth.get_user(jwt=token)
    except Exception as exc:  # invalid signature, expired, revoked, banned, GoTrue error
        logger.info(f"admin token rejected: {type(exc).__name__}")
        raise AdminUnauthorized("INVALID_OR_EXPIRED_TOKEN")
    user = getattr(response, "user", None)
    if user is None:
        raise AdminUnauthorized("INVALID_OR_EXPIRED_TOKEN")
    role = (user.app_metadata or {}).get("role")
    permissions = permissions_for(role)
    if not permissions:
        raise AdminForbidden(ErrorMessages.ADMIN_ROLE_REQUIRED.value, name=ErrorMessages.ADMIN_ROLE_REQUIRED)
    return AdminUser(id=str(user.id), email=user.email, role=role, permissions=permissions, token=token)


def admin_user(credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer)) -> AdminUser:
    """FastAPI dependency: the authenticated admin. Any admin role passes."""
    token = credentials.credentials if credentials and credentials.credentials else ""
    return resolve_admin(token)


def require_permission(permission: str) -> Callable[..., AdminUser]:
    """FastAPI dependency factory: the authenticated admin, who must hold ``permission``."""

    def dependency(user: AdminUser = Depends(admin_user)) -> AdminUser:
        if not user.has(permission):
            raise AdminForbidden(permission)
        return user

    dependency.__name__ = f"require_{permission.replace('.', '_')}"
    return dependency


DEFAULT_TRUSTED_PROXY_HOPS = 1


def _trusted_proxy_hops() -> int:
    value = os.getenv("ADMIN_TRUSTED_PROXY_HOPS")
    if value is None or not value.strip():
        return DEFAULT_TRUSTED_PROXY_HOPS
    try:
        hops = int(value)
        if hops >= 0:
            return hops
    except ValueError:
        pass
    logger.warning(f"invalid ADMIN_TRUSTED_PROXY_HOPS {value!r}, using {DEFAULT_TRUSTED_PROXY_HOPS}")
    return DEFAULT_TRUSTED_PROXY_HOPS


def _valid_ip(value: Optional[str]) -> Optional[str]:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def rate_limit_ip(request: Request) -> Optional[str]:
    """Client IP the admin login rate limit counts by (TEOS-109). Unlike ``client_ip`` it cannot be
    chosen by the caller: with ``ADMIN_TRUSTED_PROXY_HOPS`` = n (default 1, Render's proxy) it is the
    n-th X-Forwarded-For entry counted from the RIGHT, i.e. the address the outermost trusted proxy
    saw. Entries to the left of it, CF-Connecting-IP and X-Real-IP are client-controlled and ignored.
    0 hops, too few entries or an entry that is not an IP: the TCP peer."""
    peer = request.client.host if request.client else None
    hops = _trusted_proxy_hops()
    if hops == 0:
        return peer
    entries = [part.strip() for header in request.headers.getlist("x-forwarded-for")
               for part in header.split(",") if part.strip()]
    if len(entries) < hops:
        return peer
    return _valid_ip(entries[-hops]) or peer


def client_ip(request: Request) -> Optional[str]:
    """Best-effort caller IP for audit rows: CF-Connecting-IP, X-Real-IP, first X-Forwarded-For, peer."""
    headers = request.headers
    for name in ("cf-connecting-ip", "x-real-ip"):
        value = (headers.get(name) or "").strip()
        if value:
            return value
    forwarded = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else None
