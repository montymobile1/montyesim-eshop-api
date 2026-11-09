"""Authentication and authorization for the admin API.

``admin_user`` validates the bearer token with Supabase Auth on every request and reads the
role from ``app_metadata.role``, so revoking a role or banning an admin takes effect on the
next request instead of when the token expires. ``require_permission`` is the per-route check.

TEOS-255 (trusted devices): the token's ``session_id`` claim must also belong to an active
``admin_devices`` row (one indexed lookup per request), so a session opened directly against
Supabase, or a revoked device, gets 401. The token must also not be newer than the last token the API
handed to that device (``last_token_iat``): a token refreshed directly at Supabase gets 401.
"""
import base64
import ipaddress
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Optional

import jwt
from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger

from app.config.admin_permissions import permissions_for
from app.config.config import supabase_client
from app.config.constants import ErrorMessages
from app.exceptions.admin import AdminError, AdminForbidden, AdminUnauthorized, AdminUpstreamError, coded

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
    session_id: Optional[str] = None  # TEOS-255: Supabase session of the token (JWT claim)
    device_id: Optional[str] = None   # TEOS-255: the admin_devices row of that session

    def has(self, permission: str) -> bool:
        return permission in self.permissions


def _verify_signature(token: str) -> None:
    """Cheap local check before the network call; skipped when SUPABASE_JWT_SECRET is not set
    (projects on asymmetric signing keys). Supabase Auth's get_user stays the authority."""
    secret = os.getenv("SUPABASE_JWT_SECRET")
    if not secret:
        return
    jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")


#: TEOS-307: sign-in methods of a session opened from an email link (password reset, magic link, email
#: code). Such a session proves the inbox, not the password. ``POST /auth/reset-password`` accepts it only when it
#: has no ``admin_devices`` row (TEOS-255: a verify-device portal session has one). On the other admin routes only
#: ``recovery`` is refused outright; otp / magiclink sessions pass only through an active device row.
EMAIL_LINK_METHODS: FrozenSet[str] = frozenset({"recovery", "otp", "magiclink"})


def token_auth_methods(token: str) -> FrozenSet[str]:
    """The ``amr`` methods of a Supabase access token (how its session was opened); empty when the token
    cannot be read. The claims are read without checking the signature: callers still validate the token
    with Supabase Auth, this only says what kind of session it is."""
    amr = _unverified_claims(token).get("amr")
    if not isinstance(amr, list):
        return frozenset()
    return frozenset(entry.get("method") for entry in amr
                     if isinstance(entry, dict) and isinstance(entry.get("method"), str))


def is_email_link_token(token: str) -> bool:
    """True when the token's session was opened only from an email link (see ``EMAIL_LINK_METHODS``)."""
    methods = token_auth_methods(token)
    return bool(methods) and methods <= EMAIL_LINK_METHODS


def is_recovery_session(token: str) -> bool:
    """TEOS-255: True for a password reset link's session (amr contains ``recovery``): never a portal session,
    even with a device row. Every other session (password, ``otp`` or ``magiclink``: GoTrue may label the
    session of ``verify_otp(type="email")`` either way) is a portal session only through its active device
    row, which only the API's sign-in creates; one opened directly against Supabase has none."""
    return "recovery" in token_auth_methods(token)


def _unverified_claims(token: str) -> Dict[str, Any]:
    """The payload of a JWT (its middle segment), read without checking the signature; {} when unreadable.
    Only for reading claims of a token that Supabase Auth validates (``resolve_admin``) or that Supabase
    just returned (sign-in, refresh): never use it to decide that a token is genuine."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        return {}
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, TypeError):  # bad base64, bad UTF-8 or bad JSON
        return {}
    return claims if isinstance(claims, dict) else {}


def token_session_id(token: str) -> Optional[str]:
    """The ``session_id`` claim of a Supabase access token (readable even once expired), else None. The
    signature is not checked here: callers validate the token with Supabase Auth."""
    value = _unverified_claims(token).get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def token_iat(token: str) -> Optional[int]:
    """The ``iat`` claim (issued at, seconds) of a Supabase access token, else None. Not signature-checked."""
    value = _unverified_claims(token).get("iat")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


_device_service = None


def _default_session_device(session_id: str) -> Optional[Dict[str, Any]]:
    global _device_service
    if _device_service is None:  # one instance (and one database client) for every request
        from app.services.admin.trusted_device_service import TrustedDeviceService
        _device_service = TrustedDeviceService()
    return _device_service.session_device(session_id)


#: TEOS-255: ``session_id`` -> the device row of that session (active, else the latest revoked, else None).
#: Module level so tests can replace it (``monkeypatch.setattr(admin_security, "session_device_lookup", ...)``).
session_device_lookup: Callable[[str], Optional[Dict[str, Any]]] = _default_session_device


def _session_required() -> AdminError:
    return coded(AdminUnauthorized, "device_session_required", ErrorMessages.ADMIN_DEVICE_SESSION_REQUIRED)


def device_session_error(row: Optional[Dict[str, Any]], admin_id: str) -> Optional[AdminError]:
    """The 401 for a session whose device row is missing, belongs to another account, or is revoked;
    None when the row is active."""
    if not row or str(row.get("admin_id") or "").lower() != str(admin_id).lower():
        return _session_required()
    if row.get("revoked_at"):
        return coded(AdminUnauthorized, "device_revoked", ErrorMessages.ADMIN_DEVICE_REVOKED)
    trusted_until = _as_utc(row.get("trusted_until"))
    if trusted_until is not None and trusted_until <= datetime.now(timezone.utc):
        # TEOS-255: a trusted device's session ends with its trust (30 days); the next sign-in asks for a code.
        # A device signed in without trust (trusted_until null) keeps its session as long as Supabase does.
        return coded(AdminUnauthorized, "device_trust_expired", ErrorMessages.ADMIN_DEVICE_SESSION_REQUIRED)
    return None


def _as_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _default_refusal_audit(row: Dict[str, Any], admin_id: str, ip: Optional[str]) -> None:
    from app.repo.admin.base import AuditContext
    from app.services.admin.audit_service import AuditService
    AuditService().log(AuditContext(actor_id=str(admin_id), ip=ip), "admin_devices", "UPDATE",
                       record_id=str(row.get("id")), action="session_refused", reason="device_revoked",
                       device_id=str(row.get("id")), revoke_reason=row.get("revoke_reason"))


#: TEOS-255 audit "session refused because its device was revoked". Module level so tests can replace it.
refusal_audit: Callable[[Dict[str, Any], str, Optional[str]], None] = _default_refusal_audit


def audit_refused_session(row: Optional[Dict[str, Any]], admin_id: str, ip: Optional[str] = None) -> None:
    """One audit row when a revoked device's session is refused (per request or on refresh). Best effort:
    the 401 is returned anyway."""
    if not row or not row.get("revoked_at"):
        return
    try:
        refusal_audit(row, admin_id, ip)
    except Exception as exc:
        logger.error(f"admin device: refused-session audit failed: {type(exc).__name__}")


def token_newer_than_device(row: Dict[str, Any], token: str) -> bool:
    """True when the token was issued after the last token the API handed to this device (``last_token_iat``):
    it was refreshed outside the API. A row with no ``last_token_iat`` yet accepts any token."""
    last = row.get("last_token_iat")
    if last is None:
        return False
    iat = token_iat(token)
    return iat is None or iat > int(last)


def find_session_device(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """``session_device_lookup`` with a store failure turned into 502 (fail closed)."""
    if not session_id:
        return None
    try:
        return session_device_lookup(session_id)
    except AdminError:
        raise
    except Exception as exc:
        logger.error(f"admin device lookup failed: {type(exc).__name__}")
        raise AdminUpstreamError("DEVICE_LOOKUP_FAILED")


def resolve_admin(token: str) -> AdminUser:
    """Validate ``token`` with Supabase Auth and return the admin it belongs to.

    Raises AdminUnauthorized (401) for a missing, invalid, expired or revoked session or a reset-link
    session (TEOS-307), AdminForbidden (403) when the account has no admin role, and (TEOS-255) 401
    ``device_session_required`` / ``device_revoked`` when the token's session has no active device row or the
    token is newer than the last one the API handed to that device.
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
    if is_recovery_session(token):
        # TEOS-307: the session of a password reset link only sets the new password
        logger.info("admin token rejected: email link session")
        raise AdminUnauthorized("EMAIL_LINK_SESSION_NOT_ALLOWED")
    role = (user.app_metadata or {}).get("role")
    permissions = permissions_for(role)
    if not permissions:
        raise AdminForbidden(ErrorMessages.ADMIN_ROLE_REQUIRED.value, name=ErrorMessages.ADMIN_ROLE_REQUIRED)
    # TEOS-255 (D5): only sessions registered by the sign-in (an active device row) are portal sessions
    session_id = token_session_id(token)
    row = find_session_device(session_id)
    error = device_session_error(row, str(user.id))
    if error is not None:
        logger.info(f"admin token rejected: {'device revoked' if row and row.get('revoked_at') else 'no device row'}")
        audit_refused_session(row, str(user.id))
        raise error
    if token_newer_than_device(row, token):
        logger.info("admin token rejected: issued outside the API (newer than the device's last token)")
        raise _session_required()
    return AdminUser(id=str(user.id), email=user.email, role=role, permissions=permissions, token=token,
                     session_id=session_id, device_id=str(row["id"]))


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


def _trust_cf_connecting_ip() -> bool:
    value = os.getenv("ADMIN_TRUST_CF_CONNECTING_IP")
    if value is None or not value.strip():
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def _valid_ip(value: Optional[str]) -> Optional[str]:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def rate_limit_ip(request: Request) -> Optional[str]:
    """Client IP the admin login rate limit counts by (TEOS-109, TEOS-142). Unlike ``client_ip`` it
    cannot be chosen by the caller.

    1. CF-Connecting-IP, when ``ADMIN_TRUST_CF_CONNECTING_IP`` is on (default): Render is served
       through Cloudflare, which sets this header to the real client IP and overwrites any value the
       client sent. The rightmost X-Forwarded-For entry there is a Cloudflare edge address that
       changes between requests, so counting by it never reached the per-IP limit (TEOS-142).
    2. Otherwise, with ``ADMIN_TRUSTED_PROXY_HOPS`` = n (default 1) the n-th X-Forwarded-For entry
       counted from the RIGHT, i.e. the address the outermost trusted proxy saw.
    X-Real-IP and the X-Forwarded-For entries left of the trusted hop are client-controlled and
    ignored. 0 hops, too few entries or an entry that is not an IP: the TCP peer."""
    if _trust_cf_connecting_ip():
        cf_ip = _valid_ip(request.headers.get("cf-connecting-ip"))
        if cf_ip:
            return cf_ip
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
