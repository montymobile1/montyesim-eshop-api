"""TEOS-255 trusted devices, sign-in side (BE-1): HTTP contract of /admin/auth/login, /verify-device,
/resend-code, /refresh and the per-request device check on a bearer route."""
import json
from types import SimpleNamespace

import pytest

from app.config.admin_permissions import ROLE_ADMIN, permissions_for
from app.config.constants import ErrorMessages
from app.exceptions.admin import AdminError, AdminGone, AdminRateLimited, AdminValidationError, coded
from app.schemas.admin.auth import AdminProfile, AdminSession
from app.schemas.admin.trusted_devices import AdminCodeResent, AdminLoginChallenge
from tests.admin.conftest import make_auth_user
from tests.admin.services.test_trusted_devices_signin import DEVICE_PAYLOAD, FakeDevices, session_token

BASE = "/api/v1/admin/auth"
CHALLENGE_ID = "3f1c2b4a-5d6e-4f70-8a9b-0c1d2e3f4a5b"


@pytest.fixture
def auth_service(monkeypatch):
    from app.api.v1.admin import auth as auth_routes
    fake = SimpleNamespace(calls=[])
    monkeypatch.setattr(auth_routes, "service", fake)
    return fake


def _session(device_id="dev-1") -> AdminSession:
    return AdminSession(access_token="a", refresh_token="r", expires_in=3600, device_id=device_id,
                        user=AdminProfile(id="u1", email="admin@example.com", role=ROLE_ADMIN,
                                          permissions=sorted(permissions_for(ROLE_ADMIN))))


def _developer_code(response):
    return json.loads(response.json()["developerMessage"])


def test_login_of_an_untrusted_browser_answers_verification_required(api_client, auth_service):
    def login(email, password, ip, device, user_agent):
        auth_service.calls.append((device.browser, user_agent))
        return AdminLoginChallenge(challenge_id=CHALLENGE_ID, destination="a***@e***.com", code_length=6,
                                   expires_in=600, resend_available_in=60, resends_left=3)

    auth_service.login = login

    response = api_client.post(f"{BASE}/login", json={"email": "admin@example.com", "password": "secret",
                                                      "device": DEVICE_PAYLOAD}, headers={"User-Agent": "UA/1"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {"status": "verification_required", "challenge_id": CHALLENGE_ID, "channel": "email",
                    "destination": "a***@e***.com", "code_length": 6, "expires_in": 600,
                    "resend_available_in": 60, "resends_left": 3}
    assert auth_service.calls == [("Chrome", "UA/1")]


def test_login_of_a_trusted_device_answers_signed_in_with_the_device(api_client, auth_service):
    auth_service.login = lambda *args: _session()

    response = api_client.post(f"{BASE}/login", json={"email": "admin@example.com", "password": "secret",
                                                      "device": DEVICE_PAYLOAD})

    data = response.json()["data"]
    assert data["status"] == "signed_in" and data["device_id"] == "dev-1" and data["access_token"] == "a"


def test_verify_device_returns_the_session(api_client, auth_service):
    auth_service.verify_device = lambda *args: auth_service.calls.append(args[:4]) or _session()

    response = api_client.post(f"{BASE}/verify-device", json={
        "challenge_id": CHALLENGE_ID, "code": "123456", "trust_device": True, "device_name": "  Work   laptop ",
        "device": DEVICE_PAYLOAD}, headers={"X-Forwarded-For": "203.0.113.7"})

    assert response.status_code == 200
    assert response.json()["data"]["device_id"] == "dev-1"
    assert auth_service.calls == [(CHALLENGE_ID, "123456", True, "Work laptop")]


@pytest.mark.parametrize("body", [
    {"challenge_id": CHALLENGE_ID, "code": "123456", "trust_device": True},                      # no device
    {"challenge_id": CHALLENGE_ID, "code": "", "device": DEVICE_PAYLOAD},
    {"challenge_id": CHALLENGE_ID, "code": "123456", "device": DEVICE_PAYLOAD, "admin_id": "x"},  # unknown field
    {"challenge_id": CHALLENGE_ID, "code": "123456", "device": DEVICE_PAYLOAD, "device_name": "x" * 61},
])
def test_verify_device_invalid_body_is_422(api_client, auth_service, body):
    response = api_client.post(f"{BASE}/verify-device", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("error, status, expected", [
    (coded(AdminValidationError, "code_format", ErrorMessages.ADMIN_VALIDATION_FAILED, code_length=6), 422,
     {"code": "code_format", "code_length": 6}),
    (coded(AdminError, "code_invalid", ErrorMessages.ADMIN_DEVICE_CODE_INVALID, attempts_left=3), 400,
     {"code": "code_invalid", "attempts_left": 3}),
    (coded(AdminRateLimited, "too_many_attempts", ErrorMessages.ADMIN_DEVICE_TOO_MANY_ATTEMPTS), 429,
     {"code": "too_many_attempts"}),
    (coded(AdminGone, "challenge_expired", ErrorMessages.ADMIN_DEVICE_CODE_EXPIRED), 410,
     {"code": "challenge_expired"}),
])
def test_verify_device_errors_carry_the_machine_code(api_client, auth_service, error, status, expected):
    def fail(*args):
        raise error

    auth_service.verify_device = fail

    response = api_client.post(f"{BASE}/verify-device", json={"challenge_id": CHALLENGE_ID, "code": "1",
                                                              "device": DEVICE_PAYLOAD})

    assert response.status_code == status and response.json()["responseCode"] == status
    assert _developer_code(response) == expected
    assert response.json()["message"]  # translated text for the admin


def test_resend_code(api_client, auth_service):
    auth_service.resend_code = lambda challenge_id, ip: auth_service.calls.append(challenge_id) or AdminCodeResent(
        expires_in=600, resend_available_in=60, resends_left=2)

    response = api_client.post(f"{BASE}/resend-code", json={"challenge_id": CHALLENGE_ID})

    assert response.status_code == 200
    assert response.json()["data"] == {"expires_in": 600, "resend_available_in": 60, "resends_left": 2}
    assert auth_service.calls == [CHALLENGE_ID]


def test_resend_code_too_soon_is_429_with_retry_after(api_client, auth_service):
    def fail(*args):
        raise coded(AdminRateLimited, "resend_too_soon", ErrorMessages.ADMIN_DEVICE_RESEND_TOO_SOON, retry_after=42)

    auth_service.resend_code = fail

    response = api_client.post(f"{BASE}/resend-code", json={"challenge_id": CHALLENGE_ID})

    assert response.status_code == 429
    assert _developer_code(response) == {"code": "resend_too_soon", "retry_after": 42}


def test_refresh_requires_the_device(api_client, auth_service):
    assert api_client.post(f"{BASE}/refresh", json={"refresh_token": "r"}).status_code == 422


# ---------- the per-request device check (real admin_security, Supabase and the lookup faked) ----------

@pytest.fixture
def devices(monkeypatch):
    fake = FakeDevices()
    monkeypatch.setattr("app.dependencies.admin_security.session_device_lookup", fake.session_device)
    return fake


def _me(api_client, token):
    return api_client.get(f"{BASE}/me", headers={"Authorization": f"Bearer {token}"})


def test_case_18_direct_supabase_session_is_401_on_admin_routes(api_client, supabase_auth, devices):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_ADMIN))

    response = _me(api_client, session_token("direct-session"))

    assert response.status_code == 401
    assert _developer_code(response) == {"code": "device_session_required"}
    assert response.json()["message"] == "Your session has ended. Sign in again."


def test_case_07_revoked_device_is_401_device_revoked(api_client, supabase_auth, devices):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_ADMIN))
    devices.add(session_id="sess-1", revoked=True)

    response = _me(api_client, session_token("sess-1"))

    assert response.status_code == 401
    assert _developer_code(response) == {"code": "device_revoked"}


def test_session_with_an_active_device_row_passes(api_client, supabase_auth, devices):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_ADMIN))
    devices.add(session_id="otp-1")

    response = _me(api_client, session_token("otp-1", "otp"))

    assert response.status_code == 200
    assert response.json()["data"]["role"] == ROLE_ADMIN


def test_magiclink_session_with_a_device_row_passes(api_client, supabase_auth, devices):
    """GoTrue may label the verify-device session ``magiclink``: it is a portal session through its row."""
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_ADMIN))
    devices.add(session_id="link-1")

    assert _me(api_client, session_token("link-1", "magiclink")).status_code == 200
    assert _developer_code(_me(api_client, session_token("link-2", "magiclink"))) == {"code": "device_session_required"}


def test_token_refreshed_directly_at_supabase_is_401(api_client, supabase_auth, devices):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(ROLE_ADMIN))
    devices.add(session_id="sess-1", last_token_iat=1000)

    assert _me(api_client, session_token("sess-1", iat=1000)).status_code == 200
    response = _me(api_client, session_token("sess-1", iat=1500))
    assert response.status_code == 401
    assert _developer_code(response) == {"code": "device_session_required"}
