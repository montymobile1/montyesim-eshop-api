"""Fixtures for the admin API tests.

The root tests/conftest.py sets dummy Supabase/Stripe variables before any app import. Importing
app.main also imports the push-notification module, which initialises Firebase at import time and
needs a service-account file; the admin tests never send notifications, so when no Firebase app is
initialised yet a placeholder is registered and that initialisation is skipped.
"""
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


@pytest.fixture(autouse=True)
def no_local_jwt_secret(monkeypatch):
    """A developer's .env may hold SUPABASE_JWT_SECRET; tests decide per case whether it is set."""
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)


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
