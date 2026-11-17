"""The tokens of the password reset links sent from the Admins page (TEOS-337/339).

Until these bugs the token was Supabase's recovery token (``generate_link`` type recovery). Two Supabase rules
broke the TEOS-307 story:

- TEOS-337: Supabase keeps ONE such token per user and the admin sign-in replaces it (the session after the email
  code is opened through a magic link, which shares it), so signing in killed a pending reset link;
- TEOS-339: its lifetime is the Supabase project's "Email OTP Expiration" (24 h by default), not the story's 1 hour.

The API now keeps its own tokens in ``admin_password_reset``: random (``secrets``, 256 bits), stored only as
SHA-256, valid 1 hour, single use. Sending a new link cancels the admin's earlier ones; setting a new password
(from the link or the Profile page) or losing admin access cancels any pending link. Signing in touches none of
it. The password itself is still set with Supabase's admin API, and the link format (``#token=``) is unchanged.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from loguru import logger

RESET_LINK_TTL_SECONDS = 3600
#: links sent to one admin per hour (a super admin clicking "Send reset link" again and again)
MAX_LINKS_PER_HOUR = 5
TOKEN_BYTES = 32


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    """SHA-256 (hex) of a link's token: what is stored and looked up. A 256-bit random token needs no secret key
    (it cannot be guessed from its hash)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _default_repo():
    from app.repo.admin.password_reset_repo import AdminPasswordResetRepo
    return AdminPasswordResetRepo()


class PasswordResetTokens:
    def __init__(self, repo_factory: Optional[Callable[[], Any]] = None, clock: Callable[[], datetime] = utc_now):
        self._repo_factory = repo_factory
        self._repo = None
        self._clock = clock

    @property
    def repo(self):
        """Created lazily, so importing a router opens no database client."""
        if self._repo is None:
            self._repo = (self._repo_factory or _default_repo)()  # looked up here, so tests can replace it
        return self._repo

    def sent_last_hour(self, admin_id: str) -> int:
        return self.repo.sent_since(str(admin_id), self._clock() - timedelta(seconds=3600))

    def issue(self, admin_id: str, created_by: Optional[str]) -> Tuple[str, datetime, Any]:
        """A new link token for the admin, valid 1 hour. Returns the token (only ever put in the email), its expiry
        and the link's id. The admin's earlier links keep working until ``sent`` (the email may still fail).
        Raises when the store fails."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        expires_at = self._clock() + timedelta(seconds=RESET_LINK_TTL_SECONDS)
        row = self.repo.create({"admin_id": str(admin_id), "token_hash": hash_token(token), "created_by": created_by,
                                "expires_at": expires_at.isoformat()})
        return token, expires_at, row.get("id")

    def sent(self, admin_id: str, link_id: Any) -> None:
        """Best effort, once the email is on its way: the admin's earlier links stop working (only the newest
        link works)."""
        try:
            self.repo.cancel_pending(str(admin_id), self._clock(), except_id=link_id)
        except Exception as exc:
            logger.error(f"admin password reset: earlier links not cancelled: {type(exc).__name__}")

    def not_sent(self, link_id: Any) -> None:
        """Best effort: the link whose email could not be sent never works."""
        try:
            self.repo.cancel(link_id, self._clock())
        except Exception as exc:
            logger.error(f"admin password reset: unsent link not cancelled: {type(exc).__name__}")

    def claim(self, token: str) -> Optional[Dict[str, Any]]:
        """Uses the link (once): its row when the token is known, unused, not cancelled and not expired, else None.
        Raises when the store fails."""
        return self.repo.claim(hash_token(token), self._clock())

    def release(self, link_id: Any) -> None:
        """Best effort: the claimed link works again (the password was not set for a reason the admin can fix)."""
        try:
            self.repo.release(link_id)
        except Exception as exc:
            logger.error(f"admin password reset: link not released: {type(exc).__name__}")

    def cancel(self, admin_id: str, why: str) -> None:
        """Best effort: every pending link of the admin stops working (new password set, access revoked)."""
        try:
            self.repo.cancel_pending(str(admin_id), self._clock())
        except Exception as exc:
            logger.error(f"admin {why}: pending reset links not cancelled: {type(exc).__name__}")
