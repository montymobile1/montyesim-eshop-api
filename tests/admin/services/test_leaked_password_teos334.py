"""TEOS-334: leaked passwords (Have I Been Pwned range API, k-anonymity) are refused with 422 weak_password on the
admin password change and the reset link, before anything is changed or the link is used. Fail open when HIBP
can't be asked. HIBP is faked: no network."""
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config.admin_permissions import ROLE_ADMIN
from app.exceptions.admin import AdminValidationError
from app.repo.admin.base import AuditContext
from app.services.admin import leaked_password
from app.services.admin.auth_service import AdminAuthService, LoginRateLimiter
from tests.admin.conftest import FakeClock, FakeLoginAttemptStore, make_admin, make_auth_user, make_session
from tests.admin.services.test_trusted_devices_signin import FakeDevices

LEAKED = "Password123!"
CLEAN = "N3w-Passw0rd!x"
CTX = AuditContext(actor_id="11111111-1111-1111-1111-111111111111", ip="1.2.3.4")


def _sha1(password):
    return hashlib.sha1(password.encode("utf-8")).hexdigest().upper()


@pytest.fixture
def hibp(monkeypatch):
    """A fake HIBP that knows LEAKED (plus padding lines with count 0) and records the prefixes it was asked."""
    asked = []

    def fetch(prefix):
        asked.append(prefix)
        lines = ["0018A45C4D1DEF81644B54AB7F969B88D65:0"]  # padding
        digest = _sha1(LEAKED)
        if digest.startswith(prefix):
            lines.append(f"{digest[5:]}:3861493")
        return "\r\n".join(lines)
    monkeypatch.setattr(leaked_password, "range_fetcher", fetch)
    return asked


@pytest.fixture
def client():
    return MagicMock(name="supabase-client")


@pytest.fixture
def auth(client):
    limiter = LoginRateLimiter(5, 300, store_factory=lambda: FakeLoginAttemptStore(FakeClock()))
    return AdminAuthService(client_factory=lambda: client, rate_limiter=limiter, audit_factory=MagicMock,
                            device_service_factory=FakeDevices)


# ---------- the check ----------

def test_leaked_password_is_found_and_only_a_5_character_prefix_is_sent(hibp):
    assert leaked_password.is_leaked(LEAKED) is True
    assert hibp == [_sha1(LEAKED)[:5]]  # never the password nor the full hash


def test_clean_password_and_padding_lines_are_not_leaked(hibp):
    assert leaked_password.is_leaked(CLEAN) is False


@pytest.mark.parametrize("failure", [TimeoutError("slow"), ConnectionError("offline"), RuntimeError("503")])
def test_hibp_unavailable_fails_open(monkeypatch, failure):
    def broken(prefix):
        raise failure
    monkeypatch.setattr(leaked_password, "range_fetcher", broken)

    assert leaked_password.is_leaked(LEAKED) is False


@pytest.mark.parametrize("value", ["off", "false", "0", "NO"])
def test_the_check_can_be_disabled(monkeypatch, hibp, value):
    monkeypatch.setenv("ADMIN_PASSWORD_LEAK_CHECK", value)

    assert leaked_password.is_leaked(LEAKED) is False
    assert hibp == []


def test_unreadable_answer_counts_as_not_leaked():
    suffix = _sha1(LEAKED)[5:]
    assert leaked_password._count(f"{suffix}:abc", suffix) == 0
    assert leaked_password._count(None, suffix) == 0
    assert leaked_password._count(f"{suffix.lower()}:12", suffix) == 12


def test_fetch_sends_padding_and_a_generic_user_agent(monkeypatch):
    seen = {}

    def fake_get(url, timeout, headers):
        seen.update(url=url, timeout=timeout, headers=headers)
        return SimpleNamespace(text="", raise_for_status=lambda: None)
    monkeypatch.setattr(leaked_password.httpx, "get", fake_get)

    leaked_password._fetch_range("ABCDE")

    assert seen["url"] == "https://api.pwnedpasswords.com/range/ABCDE"
    assert seen["headers"]["Add-Padding"] == "true" and seen["timeout"] == leaked_password.TIMEOUT_SECONDS
    assert seen["headers"]["User-Agent"] == "admin-api-password-check"


# ---------- change password ----------

def test_change_password_to_a_leaked_one_is_422_and_nothing_is_called(auth, client, hibp):
    with pytest.raises(AdminValidationError) as err:
        auth.change_password(make_admin(), "Old-Passw0rd!x", LEAKED, "1.2.3.4", CTX)

    assert err.value.code == 422 and err.value.details == "weak_password"
    client.auth.sign_in_with_password.assert_not_called()
    client.auth.admin.update_user_by_id.assert_not_called()


def test_change_password_to_a_clean_one_goes_on(auth, client, hibp):
    client.auth.sign_in_with_password.return_value = SimpleNamespace(
        session=make_session(access_token="new-access"), user=make_auth_user(ROLE_ADMIN))

    assert auth.change_password(make_admin(), "Old-Passw0rd!x", CLEAN, "1.2.3.4", CTX).access_token == "new-access"
    client.auth.admin.update_user_by_id.assert_called_once()


# ---------- reset link ----------

def test_reset_to_a_leaked_password_is_422_and_the_link_is_not_used(auth, client, hibp):
    with pytest.raises(AdminValidationError) as err:
        auth.complete_password_reset("reset-link-token", LEAKED, "1.2.3.4")

    assert err.value.details == "weak_password"
    client.auth.admin.get_user_by_id.assert_not_called()  # the link is not used: it still works for a better password
    client.auth.admin.update_user_by_id.assert_not_called()
