"""Fixtures for the admin API tests.

The root tests/conftest.py sets dummy Supabase/Stripe variables before any app import. Importing
app.main also imports the push-notification module, which initialises Firebase at import time and
needs a service-account file; the admin tests never send notifications, so when no Firebase app is
initialised yet a placeholder is registered and that initialisation is skipped.
"""
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import firebase_admin
import pytest

if not firebase_admin._apps:
    firebase_admin._apps[firebase_admin._DEFAULT_APP_NAME] = MagicMock(name="firebase-placeholder")

from fastapi.testclient import TestClient  # noqa: E402

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN, permissions_for  # noqa: E402
from app.dependencies.admin_security import AdminUser  # noqa: E402


def make_auth_user(role=ROLE_ADMIN, user_id="11111111-1111-1111-1111-111111111111", email="admin@example.com"):
    """Stand-in for gotrue's User: only the attributes the admin code reads."""
    app_metadata = {"provider": "email"}
    if role is not None:
        app_metadata["role"] = role
    return SimpleNamespace(id=user_id, email=email, app_metadata=app_metadata)


def make_session(access_token="access-token", refresh_token="refresh-token", expires_in=3600):
    return SimpleNamespace(access_token=access_token, refresh_token=refresh_token, expires_in=expires_in)


def make_admin(role=ROLE_ADMIN, token="access-token") -> AdminUser:
    return AdminUser(id="11111111-1111-1111-1111-111111111111", email="admin@example.com", role=role,
                     permissions=permissions_for(role), token=token)


class FakeClock:
    """Seconds, moved by hand; one clock is shared like the database clock."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeLoginAttemptStore:
    """In-memory twin of admin_login_attempt_begin / _finish (supabase_ddl.sql): one
    instance shared by several LoginRateLimiter instances behaves like the table shared by workers."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.rows = {}  # id -> (ip, email, attempted_at)
        self.next_id = 1
        self.begin_calls = []
        self.finish_calls = []

    def begin(self, ip, email, limit, window_seconds):
        self.begin_calls.append((ip, email, limit, window_seconds))
        since = self.clock() - window_seconds
        self.rows = {i: row for i, row in self.rows.items() if row[2] >= since}
        ip_count = sum(1 for row in self.rows.values() if row[0] == ip)
        email_count = sum(1 for row in self.rows.values() if row[1] == email)
        if ip_count >= limit or email_count >= limit:
            return False, None
        attempt_id = self.next_id
        self.next_id += 1
        self.rows[attempt_id] = (ip, email, self.clock())
        return True, attempt_id

    def finish(self, attempt_id, outcome):
        self.finish_calls.append((attempt_id, outcome))
        row = self.rows.get(attempt_id)
        if row is None:
            return
        if outcome == "success":
            self.rows = {i: r for i, r in self.rows.items() if (r[0], r[1]) != (row[0], row[1])}
        else:
            del self.rows[attempt_id]


@pytest.fixture(autouse=True)
def no_local_jwt_secret(monkeypatch):
    """A developer's .env may hold SUPABASE_JWT_SECRET; tests decide per case whether it is set."""
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_HOPS", raising=False)


@pytest.fixture
def supabase_auth(monkeypatch):
    """Patches the Supabase client used by admin_security; returns its ``auth`` mock."""
    client = MagicMock(name="supabase-client")
    monkeypatch.setattr("app.dependencies.admin_security.supabase_client", lambda: client)
    return client.auth


@pytest.fixture
def api_client(monkeypatch):
    """TestClient on the real app. Not used as a context manager, so the scheduler lifespan never starts.

    Some consumer services read app_config while the app is imported (e.g. AuthService ->
    dcb_service_instance -> get_setting); app_config reads answer "not set" so nothing goes to the network.
    """
    monkeypatch.setattr("app.repo.config_repo.ConfigRepo.get_first_by", lambda self, *a, **k: None)
    monkeypatch.setattr("app.repo.config_repo.ConfigRepo.insert_if_absent", lambda self, *a, **k: None,
                        raising=False)
    from app.main import esim_app
    return TestClient(esim_app, raise_server_exceptions=False)


@pytest.fixture
def admin_user_obj():
    return make_admin(ROLE_ADMIN)


@pytest.fixture
def super_admin_user_obj():
    return make_admin(ROLE_SUPER_ADMIN)


@pytest.fixture(autouse=True)
def _no_leaked_password_lookup(monkeypatch):
    """TEOS-334: no test asks Have I Been Pwned; every password is "not leaked" unless a test says otherwise."""
    from app.services.admin import leaked_password
    monkeypatch.setattr(leaked_password, "range_fetcher", lambda prefix: "")
    monkeypatch.delenv("ADMIN_PASSWORD_LEAK_CHECK", raising=False)


@pytest.fixture(autouse=True)
def _no_refused_session_audit(monkeypatch):
    """TEOS-255 / TEOS-321: a refused device session writes an audit row; tests never reach the real audit store
    (the tests of that audit replace it again). The rate cap starts empty in every test."""
    import app.dependencies.admin_security as admin_security
    monkeypatch.setattr(admin_security, "refusal_audit", lambda *args, **kwargs: None)
    admin_security.refusal_dedupe.clear()


def _reset_moment(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class FakeResetRepo:
    """In-memory ``admin_password_reset`` with the repo's conditions."""

    def __init__(self, clock):
        self.clock = clock
        self.rows = {}
        self.fail = None

    def _check(self):
        if self.fail:
            raise self.fail

    def _live(self, row, now):
        return row["used_at"] is None and row["cancelled_at"] is None and _reset_moment(row["expires_at"]) > now

    def create(self, row):
        self._check()
        link_id = str(uuid.uuid4())
        self.rows[link_id] = {"id": link_id, "used_at": None, "cancelled_at": None, "created_at": self.clock(), **row}
        return dict(self.rows[link_id])

    def sent_since(self, admin_id, since):
        self._check()
        return sum(1 for r in self.rows.values() if r["admin_id"] == admin_id and r["created_at"] >= since)

    def cancel_pending(self, admin_id, now, except_id=None):
        self._check()
        hit = [r for r in self.rows.values()
               if r["admin_id"] == admin_id and self._live(r, now) and r["id"] != except_id]
        for r in hit:
            r["cancelled_at"] = now
        return len(hit)

    def cancel(self, link_id, now):
        self._check()
        self.rows[link_id]["cancelled_at"] = now

    def claim(self, token_hash, now):
        self._check()
        for r in self.rows.values():
            if r["token_hash"] == token_hash and self._live(r, now):
                r["used_at"] = now
                return dict(r)
        return None

    def release(self, link_id):
        self._check()
        row = self.rows[link_id]
        if row["cancelled_at"] is None:
            row["used_at"] = None


@pytest.fixture(autouse=True)
def _no_reset_links_store(monkeypatch):
    """TEOS-337/339: the password reset links never reach the real ``admin_password_reset`` table; each test gets
    an empty in-memory one (tests of the links pass their own)."""
    from datetime import datetime, timezone
    from app.services.admin import password_reset_tokens
    monkeypatch.setattr(password_reset_tokens, "_default_repo",
                        lambda: FakeResetRepo(lambda: datetime.now(timezone.utc)))
