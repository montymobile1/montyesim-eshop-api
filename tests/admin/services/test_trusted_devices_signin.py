"""TEOS-255 trusted devices, sign-in side (BE-1): login -> email code challenge, verify-device, resend-code,
refresh tied to the device, the per-request device-session check, and the TEOS-307 hooks.

Supabase Auth, the challenge table and TrustedDeviceService are in-memory fakes: no network, no database.
The story's test case numbers are in the test names (``case_NN``).
"""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import jwt
import pytest
from gotrue.errors import AuthApiError, AuthRetryableError
from loguru import logger

from app.config.admin_permissions import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.dependencies import admin_security
from app.dependencies.admin_security import AdminUser, resolve_admin, token_session_id
from app.exceptions.admin import AdminError
from app.repo.admin.base import AuditContext
from app.schemas.admin.auth import AdminSession
from app.schemas.admin.trusted_devices import AdminDeviceInfo, AdminLoginChallenge
from app.services.admin.auth_service import AdminAuthService, LoginRateLimiter
from app.services.admin.device_challenge_service import (DeviceChallengeService, _as_datetime,
                                                         code_length_from_env, mask_email)
from app.services.admin.device_hash import device_hash
from app.services.admin.trusted_device_service import REASON_PASSWORD_CHANGE, REASON_PASSWORD_RESET
from tests.admin.conftest import FakeClock, FakeLoginAttemptStore, make_auth_user

ADMIN_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
EMAIL = "admin@example.com"
OTHER_EMAIL = "other@example.com"
T0 = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)

DEVICE_PAYLOAD = {"device_key": "8b0c7a52-6c1e-4c5f-9b1a-2f3e4d5c6b7a", "browser": "Chrome",
                  "browser_version": "130.0", "os": "Windows", "os_version": "10.0", "cpu_cores": 8,
                  "memory_gb": 8, "touch_points": 0, "gpu": "ANGLE (Intel)", "language": "en-US",
                  "timezone": "Asia/Beirut", "screen": "1920x1080", "color_depth": 24}


def device_info(**changes) -> AdminDeviceInfo:
    return AdminDeviceInfo(**{**DEVICE_PAYLOAD, **changes})


DEVICE = device_info()


def session_token(session_id: Optional[str] = "sess-1", *methods: str, sub: str = ADMIN_ID, iat: int = 1000) -> str:
    """A Supabase-like access token (unsigned for the tests): ``session_id``, ``iat`` and ``amr`` methods."""
    claims: Dict[str, Any] = {"sub": sub, "iat": iat,
                              "amr": [{"method": m, "timestamp": 1} for m in (methods or ("password",))]}
    if session_id:
        claims["session_id"] = session_id
    return jwt.encode(claims, "test-secret-" + "k" * 32, algorithm="HS256")


def auth_response(session_id="sess-1", role=ROLE_ADMIN, user_id=ADMIN_ID, email=EMAIL, methods=("password",),
                  iat=1000):
    return SimpleNamespace(
        session=SimpleNamespace(access_token=session_token(session_id, *methods, sub=user_id, iat=iat),
                                refresh_token=f"refresh-{session_id}", expires_in=3600),
        user=make_auth_user(role, user_id=user_id, email=email))


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeChallengeRepo:
    """In-memory twin of AdminDeviceChallengeRepo (same conditional updates)."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.get_calls = 0

    def create(self, row):
        challenge_id = str(uuid.uuid4())
        self.rows[challenge_id] = {"id": challenge_id, "attempts": 0, "resend_count": 0, "consumed_at": None,
                                   "failed_at": None, "created_at": self.clock().isoformat(), **row}
        return copy.deepcopy(self.rows[challenge_id])

    def get(self, challenge_id):
        self.get_calls += 1
        row = self.rows.get(challenge_id)
        return copy.deepcopy(row) if row else None

    def sends_since(self, admin_id, since):
        return sum(1 + row["resend_count"] for row in self.rows.values()
                   if row["admin_id"] == admin_id and _as_datetime(row["created_at"]) >= since)

    def claim(self, challenge_id):
        row = self.rows.get(challenge_id)
        if (not row or row["consumed_at"] or row["failed_at"]
                or _as_datetime(row["expires_at"]) <= self.clock()):
            return None
        row["consumed_at"] = self.clock().isoformat()
        return copy.deepcopy(row)

    def failed_attempt(self, challenge_id, max_attempts):
        row = self.rows.get(challenge_id)
        if not row:
            return {"found": False}
        row["attempts"] += 1
        row["consumed_at"] = None
        if row["attempts"] >= max_attempts:
            row["failed_at"] = self.clock().isoformat()
        return {"found": True, "attempts": row["attempts"], "attempts_left": max(max_attempts - row["attempts"], 0),
                "dead": row["attempts"] >= max_attempts}

    def release(self, challenge_id):
        row = self.rows.get(challenge_id)
        if row and not row["failed_at"]:
            row["consumed_at"] = None

    def mark_failed(self, challenge_id):
        self.rows[challenge_id]["failed_at"] = self.clock().isoformat()

    def fail_open(self, admin_id):
        killed = 0
        for row in self.rows.values():
            if (row["admin_id"] == admin_id and not row["consumed_at"] and not row["failed_at"]
                    and _as_datetime(row["expires_at"]) > self.clock()):
                row["failed_at"] = self.clock().isoformat()
                killed += 1
        return killed

    def record_resend(self, challenge_id, resend_count, expires_at):
        row = self.rows.get(challenge_id)
        if not row or row["resend_count"] != resend_count or row["consumed_at"] or row["failed_at"]:
            return None
        row.update(resend_count=resend_count + 1, last_sent_at=self.clock().isoformat(),
                   expires_at=expires_at.isoformat())
        return copy.deepcopy(row)


class FakeSupabase:
    """Supabase Auth: password sign-in, the email OTP (one live code per email), sessions."""

    def __init__(self):
        self.users = {ADMIN_ID: make_auth_user(ROLE_ADMIN, user_id=ADMIN_ID, email=EMAIL),
                      OTHER_ID: make_auth_user(ROLE_ADMIN, user_id=OTHER_ID, email=OTHER_EMAIL)}
        self.codes: Dict[str, str] = {}
        self.sent: List[str] = []
        self.signed_out: List[tuple] = []
        self.next_code = 123456
        self.next_session = 1
        self.otp_error: Optional[Exception] = None
        self.verify_methods = ("otp",)  # GoTrue may also label it "magiclink"
        self.verify_error: Optional[Exception] = None
        self.during_verify = None
        self.password_response = None
        self.refresh_response = None
        self.auth = SimpleNamespace(
            sign_in_with_password=self._sign_in_with_password, sign_in_with_otp=self._sign_in_with_otp,
            verify_otp=self._verify_otp, refresh_session=lambda token: self.refresh_response,
            admin=SimpleNamespace(get_user_by_id=self._get_user_by_id, update_user_by_id=lambda uid, attrs: None,
                                  sign_out=lambda token, scope: self.signed_out.append((token, scope))))

    def __call__(self):
        return self

    def _sign_in_with_password(self, credentials):
        return self.password_response or auth_response("password-session")

    def _sign_in_with_otp(self, payload):
        assert payload["options"] == {"should_create_user": False}
        if self.otp_error:
            raise self.otp_error
        self.codes[payload["email"]] = str(self.next_code)
        self.sent.append(payload["email"])
        self.next_code += 111111

    def _get_user_by_id(self, user_id):
        return SimpleNamespace(user=self.users[user_id])

    def _verify_otp(self, payload):
        assert payload["type"] == "email"
        if self.during_verify:
            hook, self.during_verify = self.during_verify, None
            hook()
        if self.verify_error:
            raise self.verify_error
        if self.codes.get(payload["email"]) != payload["token"]:
            raise AuthApiError("Token has expired or is invalid", 403, "otp_expired")
        del self.codes[payload["email"]]
        user = next(u for u in self.users.values() if u.email == payload["email"])
        session_id = f"otp-session-{self.next_session}"
        self.next_session += 1
        return auth_response(session_id, role=user.app_metadata.get("role"), user_id=user.id, email=user.email,
                             methods=self.verify_methods, iat=2000 + self.next_session)


class FakeDevices:
    """TrustedDeviceService stand-in: rows keyed by id, trusted per (admin_id, hash)."""

    def __init__(self):
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []
        self.fail: Optional[Exception] = None
        self.fail_only: Optional[set] = None  # with ``fail``: only these methods fail

    def add(self, admin_id=ADMIN_ID, info=DEVICE, trusted=True, session_id="sess-1", revoked=False,
            last_token_iat=None):
        row_id = f"dev-{len(self.rows) + 1}"
        self.rows[row_id] = {"id": row_id, "admin_id": admin_id, "device_hash": device_hash(info),
                             "trusted": trusted, "session_id": session_id, "last_token_iat": last_token_iat,
                             "revoked_at": T0.isoformat() if revoked else None}
        return self.rows[row_id]

    def _call(self, *call):
        self.calls.append(call)
        if self.fail and (self.fail_only is None or call[0] in self.fail_only):
            raise self.fail

    def record_token(self, device_id, iat):
        """Same rule as the repo's conditional update: keep the max."""
        self._call("record_token", device_id, iat)
        row = self.rows.get(device_id)
        if row is not None and iat is not None and (row["last_token_iat"] is None or row["last_token_iat"] < iat):
            row["last_token_iat"] = iat

    def find_trusted(self, admin_id, hash_):
        self._call("find_trusted", admin_id, hash_)
        return next((dict(r) for r in self.rows.values() if r["admin_id"] == admin_id and r["device_hash"] == hash_
                     and r["trusted"] and not r["revoked_at"]), None)

    def signed_in_trusted(self, row, session_id, ip, info):
        self._call("signed_in_trusted", row["id"], session_id, ip)
        self.rows[row["id"]]["session_id"] = session_id
        return dict(self.rows[row["id"]])

    def register_verified(self, admin_id, email, hash_, info, session_id, ip, trust, name, ctx):
        self._call("register_verified", admin_id, email, hash_, session_id, ip, trust, name, ctx.actor_id)
        row = self.add(admin_id=admin_id, info=info, trusted=trust, session_id=session_id)
        return dict(row)

    def session_device(self, session_id):
        self._call("session_device", session_id)
        rows = [r for r in self.rows.values() if r["session_id"] == session_id]
        active = [r for r in rows if not r["revoked_at"]]
        return dict((active or rows or [None])[0]) if rows else None

    def move_session(self, device_id, new_session_id):
        self._call("move_session", device_id, new_session_id)
        if device_id in self.rows:
            self.rows[device_id]["session_id"] = new_session_id

    def clear_session(self, session_id):
        self._call("clear_session", session_id)

    def touch(self, device_id, ip):
        self._call("touch", device_id, ip)

    def revoke_all(self, admin_id, reason, ctx, except_device_id=None):
        self._call("revoke_all", admin_id, reason, except_device_id)
        return 0

    def revoke_row(self, row_id):
        self.rows[row_id]["revoked_at"] = T0.isoformat()


class FakeAudit:
    def __init__(self):
        self.rows = []

    def log(self, ctx, table, operation, **kwargs):
        self.rows.append((ctx, table, operation, kwargs))

    def actions(self):
        return [row[3].get("action") for row in self.rows]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def supabase():
    return FakeSupabase()


@pytest.fixture
def challenge_repo(clock):
    return FakeChallengeRepo(clock)


@pytest.fixture
def devices():
    return FakeDevices()


@pytest.fixture
def audit():
    return FakeAudit()


@pytest.fixture
def challenges(supabase, challenge_repo, audit, clock):
    return DeviceChallengeService(client_factory=supabase, repo_factory=lambda: challenge_repo,
                                  audit_factory=lambda: audit, clock=clock, code_length=6)


@pytest.fixture
def service(supabase, devices, audit, challenges):
    limiter = LoginRateLimiter(5, 300, store_factory=lambda: FakeLoginAttemptStore(FakeClock()))
    return AdminAuthService(client_factory=supabase, rate_limiter=limiter, audit_factory=lambda: audit,
                            session_revoker=lambda uid: {"sessions": 1}, device_service_factory=lambda: devices,
                            challenge_service=challenges)


def _login(service, info=DEVICE, ip="1.2.3.4") -> AdminLoginChallenge:
    result = service.login(EMAIL, "secret", ip, info, "Mozilla/5.0")
    assert isinstance(result, AdminLoginChallenge)
    return result


def _code(supabase, email=EMAIL) -> str:
    return supabase.codes[email]


def _raises(call, status, code=None, **extra) -> Dict[str, Any]:
    with pytest.raises(AdminError) as err:
        call()
    assert err.value.code == status
    if code is None:
        return {}
    details = json.loads(err.value.details)
    assert details["code"] == code
    for key, value in extra.items():
        assert details[key] == value
    return details


# ---------- login ----------

def test_case_01_untrusted_device_gets_a_challenge_no_tokens_one_email(service, supabase, challenge_repo, audit):
    result = _login(service)

    assert result.status == "verification_required" and result.channel == "email"
    assert result.destination == "a***@e***.com"
    assert (result.code_length, result.expires_in, result.resend_available_in, result.resends_left) == (6, 600, 60, 3)
    assert "access_token" not in result.model_dump()
    assert supabase.sent == [EMAIL]
    # the password session is signed out: it is never handed out
    assert supabase.signed_out == [(auth_response("password-session").session.access_token, "local")]
    row = challenge_repo.rows[result.challenge_id]
    assert row["admin_id"] == ADMIN_ID and row["device_hash"] == device_hash(DEVICE)
    assert row["ip"] == "1.2.3.4" and row["user_agent"] == "Mozilla/5.0"
    assert row["device_info"]["browser"] == "Chrome" and "device_key" not in row["device_info"]
    assert _as_datetime(row["expires_at"]) == T0 + timedelta(minutes=10)
    assert audit.actions() == ["code_sent"]


def test_case_04_trusted_device_gets_the_session_without_email(service, supabase, devices, audit):
    row = devices.add(session_id="old-session")

    result = service.login(EMAIL, "secret", "1.2.3.4", DEVICE)

    assert isinstance(result, AdminSession)
    assert result.status == "signed_in" and result.device_id == row["id"]
    assert supabase.sent == [] and supabase.signed_out == []
    assert ("signed_in_trusted", row["id"], "password-session", "1.2.3.4") in devices.calls
    assert audit.actions() == []  # the device_signed_in row is written by signed_in_trusted (BE-2)


def test_case_19_another_admins_device_values_are_not_trusted(service, supabase, devices):
    devices.add(admin_id=OTHER_ID)  # same browser values, other admin

    _login(service)

    assert ("find_trusted", ADMIN_ID, device_hash(DEVICE)) in devices.calls
    assert supabase.sent == [EMAIL]


def test_case_20_expired_trust_asks_for_a_code(service, supabase, devices):
    devices.add(trusted=False)  # find_trusted (BE-2) answers None once trusted_until is past

    _login(service)

    assert supabase.sent == [EMAIL]


def test_case_22_another_browser_family_asks_for_a_code(service, supabase, devices):
    devices.add()
    firefox = device_info(browser="Firefox")

    assert device_hash(firefox) != device_hash(DEVICE)
    _login(service, info=firefox)
    assert supabase.sent == [EMAIL]


def test_login_failure_of_the_device_store_signs_the_session_out(service, supabase, devices):
    devices.fail = RuntimeError("db down")

    with pytest.raises(RuntimeError):
        service.login(EMAIL, "secret", None, DEVICE)

    assert len(supabase.signed_out) == 1


def test_login_of_a_non_admin_is_403_before_any_device_work(service, supabase, devices):
    supabase.password_response = auth_response("s", role=None)

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 403)

    assert devices.calls == [] and supabase.sent == []


def test_case_29_email_down_is_503_and_the_challenge_is_dead(service, supabase, challenge_repo):
    supabase.otp_error = AuthApiError("Error sending magic link email", 500, "unexpected_failure")

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 503, "email_unavailable")

    (row,) = challenge_repo.rows.values()
    assert row["failed_at"] is not None
    supabase.otp_error = None
    _raises(lambda: service.verify_device(row["id"], "123456", True, None, DEVICE, None), 410, "challenge_closed")
    _raises(lambda: service.resend_code(row["id"], None), 410, "challenge_closed")


def test_supabase_send_limit_on_login_is_429_too_soon_not_an_outage(service, supabase, challenge_repo):
    # Supabase allows one OTP email per user per 60 s: a second login from a new browser within the minute
    supabase.otp_error = AuthApiError("For security purposes, you can only request this after 42 seconds.",
                                      429, "over_email_send_rate_limit")

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "resend_too_soon", retry_after=60)

    (row,) = challenge_repo.rows.values()
    assert row["failed_at"] is not None  # no code was sent for it: the admin signs in again a minute later


def test_supabase_send_limit_on_resend_keeps_the_challenge_alive(service, supabase, challenge_repo, clock):
    challenge = _login(service)
    clock.advance(61)
    supabase.otp_error = AuthApiError("email rate limit exceeded", 429, "over_email_send_rate_limit")

    _raises(lambda: service.resend_code(challenge.challenge_id, None), 429, "resend_too_soon", retry_after=60)

    assert challenge_repo.rows[challenge.challenge_id]["failed_at"] is None
    supabase.otp_error = None
    clock.advance(61)
    service.resend_code(challenge.challenge_id, None)


def test_case_26_sixth_code_in_an_hour_is_429_without_email(service, supabase, challenge_repo, clock):
    for _ in range(5):
        _login(service)
        clock.advance(60)
    sent = len(supabase.sent)

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit", retry_after=3600)

    assert len(supabase.sent) == sent == 5
    assert len(challenge_repo.rows) == 5
    clock.advance(3600)  # the window moves on
    _login(service)


def test_resends_count_in_the_hourly_limit(service, supabase, clock):
    challenge = _login(service)
    for _ in range(3):
        clock.advance(60)
        service.resend_code(challenge.challenge_id, None)
    clock.advance(1)
    _login(service)  # 4 codes + this one = 5

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit")


def test_a_new_challenge_closes_the_admins_older_open_ones(service, supabase, challenge_repo, clock):
    """Supabase keeps one code per user: only the newest challenge may take guesses (finding 4)."""
    first = _login(service)
    clock.advance(61)
    second = _login(service, info=device_info(browser="Edge"))

    assert challenge_repo.rows[first.challenge_id]["failed_at"] is not None
    assert challenge_repo.rows[second.challenge_id]["failed_at"] is None
    _raises(lambda: service.verify_device(first.challenge_id, _code(supabase), True, None, DEVICE, None), 410,
            "challenge_closed")
    assert service.verify_device(second.challenge_id, _code(supabase), True, None,
                                 device_info(browser="Edge"), None).access_token


def test_older_open_challenges_are_closed_even_when_the_login_gets_429(service, supabase, challenge_repo, clock):
    first = _login(service)
    for _ in range(4):
        clock.advance(61)
        _login(service)
    clock.advance(61)
    open_before = [r for r in challenge_repo.rows.values() if not r["failed_at"]]
    assert len(open_before) == 1

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit")

    assert all(r["failed_at"] for r in challenge_repo.rows.values())
    assert challenge_repo.rows[first.challenge_id]["failed_at"]


def test_fail_open_leaves_consumed_expired_and_other_admins_challenges(challenge_repo, clock):
    def row(admin_id=ADMIN_ID, **extra):
        return challenge_repo.create({"admin_id": admin_id, "device_hash": "h", "device_info": {},
                                      "expires_at": (clock() + timedelta(minutes=10)).isoformat(),
                                      "last_sent_at": clock().isoformat(), **extra})["id"]

    open_id, other_admin = row(), row(admin_id=OTHER_ID)
    consumed = row(consumed_at=clock().isoformat())
    expired = row(expires_at=(clock() - timedelta(seconds=1)).isoformat())

    assert challenge_repo.fail_open(ADMIN_ID) == 1
    assert challenge_repo.rows[open_id]["failed_at"]
    assert not any(challenge_repo.rows[i]["failed_at"] for i in (other_admin, consumed, expired))


def test_gpu_language_or_memory_changes_keep_the_same_device(service, supabase, devices):
    """Only device_key, browser and OS are hashed: a GPU switch, a new language or more memory is the same
    browser (no code asked)."""
    row = devices.add()
    changed = device_info(gpu="NVIDIA RTX", language="fr-FR", memory_gb=32, cpu_cores=16, touch_points=5)

    assert device_hash(changed) == device_hash(DEVICE)
    assert service.login(EMAIL, "secret", None, changed).device_id == row["id"]
    assert supabase.sent == []


# ---------- verify ----------

def test_case_02_correct_code_with_trust_gives_the_session_and_a_trusted_row(service, supabase, devices):
    challenge = _login(service)

    session = service.verify_device(challenge.challenge_id, _code(supabase), True, "Work laptop", DEVICE, "1.2.3.4")

    assert session.status == "signed_in" and session.access_token and session.refresh_token
    assert token_session_id(session.access_token) == "otp-session-1"
    call = next(c for c in devices.calls if c[0] == "register_verified")
    assert call == ("register_verified", ADMIN_ID, EMAIL, device_hash(DEVICE), "otp-session-1", "1.2.3.4", True,
                    "Work laptop", ADMIN_ID)
    assert session.device_id == devices.rows[session.device_id]["id"]


def test_case_03_correct_code_without_trust_registers_an_untrusted_row(service, supabase, devices):
    challenge = _login(service)

    session = service.verify_device(challenge.challenge_id, _code(supabase), False, None, DEVICE, None)

    assert devices.rows[session.device_id]["trusted"] is False
    supabase.sent.clear()
    _login(service)  # the next login asks again
    assert supabase.sent == [EMAIL]


@pytest.mark.parametrize("code", ["12345", "1234567", "12a456", "", "١٢٣٤٥٦", " 12345"])
def test_case_28_code_of_the_wrong_format_is_422_before_any_lookup(service, supabase, challenge_repo, code):
    challenge = _login(service)
    gets = challenge_repo.get_calls

    _raises(lambda: service.verify_device(challenge.challenge_id, code, True, None, DEVICE, None), 422,
            "code_format", code_length=6)

    assert challenge_repo.get_calls == gets


def test_code_length_follows_the_setting(monkeypatch):
    monkeypatch.setenv("ADMIN_DEVICE_CODE_LENGTH", "8")
    assert code_length_from_env() == 8
    for bad in ("abc", "4", "11"):
        monkeypatch.setenv("ADMIN_DEVICE_CODE_LENGTH", bad)
        assert code_length_from_env() == 6
    monkeypatch.delenv("ADMIN_DEVICE_CODE_LENGTH")
    assert code_length_from_env() == 6


def test_case_14_wrong_codes_count_down_then_429_and_the_challenge_is_burned(service, supabase, challenge_repo):
    challenge = _login(service)
    good = _code(supabase)

    for left in (4, 3, 2, 1):
        _raises(lambda: service.verify_device(challenge.challenge_id, "000000", True, None, DEVICE, None), 400,
                "code_invalid", attempts_left=left)
    _raises(lambda: service.verify_device(challenge.challenge_id, "000000", True, None, DEVICE, None), 429,
            "too_many_attempts")

    assert challenge_repo.rows[challenge.challenge_id]["failed_at"] is not None
    _raises(lambda: service.verify_device(challenge.challenge_id, good, True, None, DEVICE, None), 410,
            "challenge_closed")


def test_case_15_code_older_than_10_minutes_is_410_expired(service, supabase, clock):
    challenge = _login(service)
    clock.advance(600)

    _raises(lambda: service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None), 410,
            "challenge_expired")


def test_case_16_consumed_challenge_is_410_closed(service, supabase):
    challenge = _login(service)
    code = _code(supabase)
    service.verify_device(challenge.challenge_id, code, True, None, DEVICE, None)

    _raises(lambda: service.verify_device(challenge.challenge_id, code, True, None, DEVICE, None), 410,
            "challenge_closed")


@pytest.mark.parametrize("challenge_id", ["not-a-uuid", str(uuid.uuid4())])
def test_unknown_challenge_is_410_closed(service, challenge_id):
    _raises(lambda: service.verify_device(challenge_id, "123456", True, None, DEVICE, None), 410, "challenge_closed")
    _raises(lambda: service.resend_code(challenge_id, None), 410, "challenge_closed")


def test_case_17_admin_a_challenge_with_admin_b_code_is_400(service, supabase):
    challenge_a = _login(service)
    supabase.password_response = auth_response("pw-b", user_id=OTHER_ID, email=OTHER_EMAIL)
    service.login(OTHER_EMAIL, "secret", None, DEVICE)
    code_b = _code(supabase, OTHER_EMAIL)

    _raises(lambda: service.verify_device(challenge_a.challenge_id, code_b, True, None, DEVICE, None), 400,
            "code_invalid", attempts_left=4)


def test_verify_from_another_browser_is_400_device_mismatch(service, supabase, challenge_repo):
    challenge = _login(service)

    _raises(lambda: service.verify_device(challenge.challenge_id, _code(supabase), True, None,
                                          device_info(device_key=str(uuid.uuid4())), None), 400, "device_mismatch")

    assert challenge_repo.rows[challenge.challenge_id]["attempts"] == 0


@pytest.mark.parametrize("error", [AuthApiError("boom", 503, "unexpected_failure"), AuthRetryableError("down", 0)])
def test_supabase_down_during_verify_is_502_and_the_attempt_is_not_counted(service, supabase, challenge_repo, error):
    challenge = _login(service)
    supabase.verify_error = error

    with pytest.raises(AdminError) as err:
        service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None)

    assert err.value.code == 502 and err.value.details == "AUTH_SERVICE_UNAVAILABLE"
    row = challenge_repo.rows[challenge.challenge_id]
    assert row["consumed_at"] is None and row["attempts"] == 0
    supabase.verify_error = None
    assert service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None).access_token


def test_case_30_two_verifies_at_once_give_one_session_and_one_410(service, supabase):
    challenge = _login(service)
    code = _code(supabase)
    second = {}

    def parallel_verify():  # runs while the first verify is waiting for Supabase
        with pytest.raises(AdminError) as err:
            service.verify_device(challenge.challenge_id, code, True, None, DEVICE, None)
        second["error"] = err.value

    supabase.during_verify = parallel_verify
    session = service.verify_device(challenge.challenge_id, code, True, None, DEVICE, None)

    assert session.access_token
    assert second["error"].code == 410 and json.loads(second["error"].details)["code"] == "challenge_closed"


def test_verify_of_an_account_that_lost_the_admin_role_is_403_and_signed_out(service, supabase, devices):
    challenge = _login(service)
    supabase.users[ADMIN_ID] = make_auth_user(None, user_id=ADMIN_ID, email=EMAIL)
    supabase.signed_out.clear()

    _raises(lambda: service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None), 403)

    assert len(supabase.signed_out) == 1
    assert not any(c[0] == "register_verified" for c in devices.calls)


def test_verify_signs_the_session_out_when_the_device_row_cannot_be_written(service, supabase, devices):
    challenge = _login(service)
    supabase.signed_out.clear()
    devices.fail = RuntimeError("db down")

    with pytest.raises(RuntimeError):
        service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None)

    assert len(supabase.signed_out) == 1


# ---------- resend ----------

def test_case_13_resend_sends_a_new_code_and_the_old_one_stops_working(service, supabase, challenge_repo, clock,
                                                                       audit):
    challenge = _login(service)
    old = _code(supabase)
    clock.advance(61)

    result = service.resend_code(challenge.challenge_id, "1.2.3.4")

    assert (result.expires_in, result.resend_available_in, result.resends_left) == (600, 60, 2)
    new = _code(supabase)
    assert new != old and supabase.sent == [EMAIL, EMAIL]
    row = challenge_repo.rows[challenge.challenge_id]
    assert _as_datetime(row["expires_at"]) == clock.now + timedelta(minutes=10)  # expiry restarts
    _raises(lambda: service.verify_device(challenge.challenge_id, old, True, None, DEVICE, None), 400, "code_invalid")
    assert service.verify_device(challenge.challenge_id, new, True, None, DEVICE, None).access_token
    assert audit.actions() == ["code_sent", "code_resent", "code_failed"]


def test_resend_too_soon_then_limit(service, supabase, clock):
    challenge = _login(service)
    clock.advance(20)

    _raises(lambda: service.resend_code(challenge.challenge_id, None), 429, "resend_too_soon", retry_after=40)
    for left in (2, 1, 0):
        clock.advance(60)
        assert service.resend_code(challenge.challenge_id, None).resends_left == left
    clock.advance(60)
    _raises(lambda: service.resend_code(challenge.challenge_id, None), 429, "resend_limit")
    assert len(supabase.sent) == 4


def test_resend_of_a_consumed_challenge_is_410(service, supabase, clock):
    challenge = _login(service)
    service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None)
    clock.advance(61)
    _raises(lambda: service.resend_code(challenge.challenge_id, None), 410, "challenge_closed")


def test_case_14_an_expired_code_can_be_resent_and_the_10_minutes_restart(service, supabase, clock):
    """Story TEOS-256 case 14: "Code expired" with a Resend option."""
    challenge = _login(service)
    clock.advance(601)
    _raises(lambda: service.verify_device(challenge.challenge_id, "123456", True, None, DEVICE, None),
            410, "challenge_expired")

    resent = service.resend_code(challenge.challenge_id, None)

    assert resent.expires_in == 600 and resent.resends_left == 2
    session = service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None)
    assert session.access_token


def test_expired_code_with_no_resends_left_is_429_resend_limit(service, supabase, clock):
    challenge = _login(service)
    for _ in range(3):
        clock.advance(61)
        service.resend_code(challenge.challenge_id, None)
    clock.advance(601)
    _raises(lambda: service.resend_code(challenge.challenge_id, None), 429, "resend_limit")


def test_parallel_resend_that_loses_the_race_is_429(service, challenge_repo, clock):
    challenge = _login(service)
    clock.advance(61)
    challenge_repo.record_resend = lambda *a: None  # another resend counted first

    _raises(lambda: service.resend_code(challenge.challenge_id, None), 429, "resend_too_soon", retry_after=60)


def test_resend_with_email_down_is_503_and_kills_the_challenge(service, supabase, challenge_repo, clock):
    challenge = _login(service)
    clock.advance(61)
    supabase.otp_error = AuthRetryableError("smtp down", 0)

    _raises(lambda: service.resend_code(challenge.challenge_id, None), 503, "email_unavailable")

    assert challenge_repo.rows[challenge.challenge_id]["failed_at"] is not None


# ---------- case 12 / 27: audit and logs ----------

def test_case_12_each_sign_in_event_writes_one_audit_row(service, supabase, audit, devices, clock):
    challenge = _login(service)
    with pytest.raises(AdminError):
        service.verify_device(challenge.challenge_id, "000000", True, None, DEVICE, None)
    clock.advance(61)
    service.resend_code(challenge.challenge_id, None)
    devices.add(info=device_info(browser="Edge"))
    service.login(EMAIL, "secret", None, device_info(browser="Edge"))

    # device_signed_in / device_verified rows are written by TrustedDeviceService (BE-2)
    assert audit.actions() == ["code_sent", "code_failed", "code_resent"]
    assert {row[1] for row in audit.rows} == {"admin_devices"}
    failed = audit.rows[1][3]
    assert failed["attempts"] == 1 and failed["attempts_left"] == 4 and failed["locked"] is False


def test_case_27_the_code_never_appears_in_logs_responses_or_audit_rows(service, supabase, audit, clock):
    captured: List[str] = []
    sink = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    outputs: List[str] = []
    codes: List[str] = []
    try:
        challenge = _login(service)
        outputs.append(challenge.model_dump_json())
        codes.append(_code(supabase))
        for wrong in ("000000", "111111"):
            with pytest.raises(AdminError) as err:
                service.verify_device(challenge.challenge_id, wrong, True, None, DEVICE, None)
            outputs.append(err.value.details)
        clock.advance(61)
        outputs.append(service.resend_code(challenge.challenge_id, None).model_dump_json())
        codes.append(_code(supabase))
        supabase.verify_error = AuthApiError("boom", 500, "unexpected_failure")
        with pytest.raises(AdminError) as err:
            service.verify_device(challenge.challenge_id, codes[-1], True, None, DEVICE, None)
        outputs.append(err.value.details)
        supabase.verify_error = None
        outputs.append(service.verify_device(challenge.challenge_id, codes[-1], True, None, DEVICE,
                                             None).model_dump_json())
    finally:
        logger.remove(sink)

    assert captured, "the flow is expected to log something"
    haystacks = captured + outputs + [repr(row) for row in audit.rows]
    for code in codes:
        assert not any(code in text for text in haystacks)


# ---------- the per-request device-session check (resolve_admin) ----------

@pytest.fixture
def lookup(monkeypatch, devices):
    monkeypatch.setattr(admin_security, "session_device_lookup", devices.session_device)
    return devices


def _get_user(supabase_auth, role=ROLE_ADMIN, user_id=ADMIN_ID):
    supabase_auth.get_user.return_value = SimpleNamespace(user=make_auth_user(role, user_id=user_id))


def test_session_with_an_active_device_row_is_accepted(supabase_auth, lookup):
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1")

    user = resolve_admin(session_token("sess-1"))

    assert (user.session_id, user.device_id) == ("sess-1", row["id"])


def test_otp_session_is_accepted_through_its_device_row(supabase_auth, lookup):
    _get_user(supabase_auth)
    lookup.add(session_id="otp-1")

    assert resolve_admin(session_token("otp-1", "otp")).role == ROLE_ADMIN


@pytest.mark.parametrize("token", [session_token("direct", "password"), session_token("direct", "otp"),
                                   session_token(None, "password"), "not-a-jwt"])
def test_case_18_direct_supabase_session_is_401_device_session_required(supabase_auth, lookup, token):
    _get_user(supabase_auth)

    _raises(lambda: resolve_admin(token), 401, "device_session_required")


@pytest.mark.parametrize("methods", [("recovery",), ("password", "recovery"), ("magiclink", "recovery")])
def test_reset_link_sessions_stay_refused_even_with_a_row(supabase_auth, lookup, methods):
    _get_user(supabase_auth)
    lookup.add(session_id="link")

    with pytest.raises(AdminError) as err:
        resolve_admin(session_token("link", *methods))

    assert err.value.code == 401 and err.value.details == "EMAIL_LINK_SESSION_NOT_ALLOWED"


@pytest.mark.parametrize("methods", [("magiclink",), ("otp",)])
def test_link_or_otp_session_without_a_device_row_is_401_device_session_required(supabase_auth, lookup, methods):
    _get_user(supabase_auth)

    _raises(lambda: resolve_admin(session_token("direct-link", *methods)), 401, "device_session_required")


@pytest.mark.parametrize("methods", [("magiclink",), ("otp",)])
def test_verify_device_session_works_on_admin_routes_and_refresh_whatever_its_amr(service, supabase, devices, lookup,
                                                                                  supabase_auth, methods):
    """GoTrue may label the session of verify_otp(type="email") ``magiclink`` or ``otp``: both are portal
    sessions through their device row."""
    supabase.verify_methods = methods
    challenge = _login(service)
    session = service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None)
    _get_user(supabase_auth)

    assert resolve_admin(session.access_token).device_id == session.device_id
    supabase.refresh_response = auth_response(token_session_id(session.access_token), methods=methods, iat=5000)
    refreshed = service.refresh("refresh-token", DEVICE)
    assert refreshed.device_id == session.device_id
    assert resolve_admin(refreshed.access_token).device_id == session.device_id


def test_device_row_of_another_admin_is_refused(supabase_auth, lookup):
    _get_user(supabase_auth)
    lookup.add(admin_id=OTHER_ID, session_id="sess-1")

    _raises(lambda: resolve_admin(session_token("sess-1")), 401, "device_session_required")


def test_case_07_and_31_revoked_device_finishes_the_request_then_401(supabase_auth, lookup):
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1")

    in_flight = resolve_admin(session_token("sess-1"))  # the request already passed the check
    lookup.revoke_row(row["id"])                         # revoked meanwhile

    assert in_flight.device_id == row["id"]
    _raises(lambda: resolve_admin(session_token("sess-1")), 401, "device_revoked")


def test_session_of_a_device_whose_trust_expired_is_401(supabase_auth, lookup):
    """Story: the session of a trusted device ends with its 30 days; untrusted devices (null) are not affected."""
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1")
    row["trusted_until"] = "2000-01-01T00:00:00+00:00"
    _raises(lambda: resolve_admin(session_token("sess-1")), 401, "device_trust_expired")

    row["trusted_until"] = "2999-01-01T00:00:00Z"
    assert resolve_admin(session_token("sess-1")).device_id == row["id"]
    row["trusted_until"] = None
    assert resolve_admin(session_token("sess-1")).device_id == row["id"]


def test_refresh_of_a_device_whose_trust_expired_is_401(service, supabase, devices):
    row = devices.add(session_id="sess-9")
    row["trusted_until"] = "2000-01-01T00:00:00+00:00"
    supabase.refresh_response = auth_response("sess-9")

    _raises(lambda: service.refresh("refresh-token", DEVICE, "1.2.3.4"), 401, "device_trust_expired")


def test_refused_revoked_device_writes_one_audit_row(supabase_auth, lookup, monkeypatch):
    audits = []
    monkeypatch.setattr(admin_security, "refusal_audit",
                        lambda row, admin_id, ip: audits.append((row["id"], admin_id, row["revoked_at"] is not None)))
    _get_user(supabase_auth)
    row = lookup.add(session_id="sess-1", revoked=True)

    _raises(lambda: resolve_admin(session_token("sess-1")), 401, "device_revoked")
    assert audits == [(row["id"], ADMIN_ID, True)]

    _raises(lambda: resolve_admin(session_token("no-row")), 401, "device_session_required")
    assert len(audits) == 1  # only revoked devices: a session without a row is not this event


def test_refused_session_audit_failure_still_gives_the_401(supabase_auth, lookup, monkeypatch):
    def broken(row, admin_id, ip):
        raise RuntimeError("audit store down")
    monkeypatch.setattr(admin_security, "refusal_audit", broken)
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1", revoked=True)

    _raises(lambda: resolve_admin(session_token("sess-1")), 401, "device_revoked")


def test_token_refreshed_outside_the_api_is_401(supabase_auth, lookup):
    """Same session, newer iat than the last token the API handed out: refreshed directly at Supabase."""
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1", last_token_iat=1000)

    assert resolve_admin(session_token("sess-1", iat=1000)).session_id == "sess-1"  # the API's own token
    assert resolve_admin(session_token("sess-1", iat=900)).session_id == "sess-1"   # an older one, still valid
    _raises(lambda: resolve_admin(session_token("sess-1", iat=1001)), 401, "device_session_required")


def test_row_without_last_token_iat_accepts_any_token(supabase_auth, lookup):
    _get_user(supabase_auth)
    lookup.add(session_id="sess-1")

    assert resolve_admin(session_token("sess-1", iat=99999)).session_id == "sess-1"


def test_every_token_the_api_hands_out_is_recorded(service, supabase, devices, lookup, supabase_auth):
    _get_user(supabase_auth)
    challenge = _login(service)
    verified = service.verify_device(challenge.challenge_id, _code(supabase), True, None, DEVICE, None)
    row_id = verified.device_id
    assert devices.rows[row_id]["last_token_iat"] == admin_security.token_iat(verified.access_token)

    # trusted login on the same browser
    supabase.password_response = auth_response("pw-2", iat=3000)
    assert service.login(EMAIL, "secret", None, DEVICE).device_id == row_id
    assert devices.rows[row_id]["last_token_iat"] == 3000

    # the API's own refresh: recorded, so the new token works; a direct-Supabase refresh after it does not
    supabase.refresh_response = auth_response("pw-2", iat=3600)
    refreshed = service.refresh("refresh-token", DEVICE)
    assert devices.rows[row_id]["last_token_iat"] == 3600
    assert resolve_admin(refreshed.access_token).device_id == row_id
    _raises(lambda: resolve_admin(session_token("pw-2", iat=4200)), 401, "device_session_required")


def test_out_of_order_token_records_keep_the_max(devices):
    row = devices.add(session_id="sess-1")

    devices.record_token(row["id"], 2000)
    devices.record_token(row["id"], 1500)  # an older refresh stored late
    devices.record_token(row["id"], None)

    assert devices.rows[row["id"]]["last_token_iat"] == 2000


def test_refresh_signs_out_when_the_token_cannot_be_recorded(service, supabase, devices):
    devices.add(session_id="sess-9")
    supabase.refresh_response = auth_response("sess-9")
    devices.fail, devices.fail_only = RuntimeError("db down"), {"record_token"}

    with pytest.raises(RuntimeError):
        service.refresh("refresh-token", DEVICE)

    assert supabase.signed_out == [(supabase.refresh_response.session.access_token, "local")]


def test_device_lookup_failure_is_502_fail_closed(supabase_auth, monkeypatch):
    _get_user(supabase_auth)

    def broken(session_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(admin_security, "session_device_lookup", broken)

    with pytest.raises(AdminError) as err:
        resolve_admin(session_token("sess-1"))
    assert err.value.code == 502


def test_non_admin_is_still_403(supabase_auth, lookup):
    _get_user(supabase_auth, role=None)

    _raises(lambda: resolve_admin(session_token("sess-1")), 403)


# ---------- refresh ----------

def test_refresh_from_the_device_browser_returns_the_session(service, supabase, devices):
    row = devices.add(session_id="sess-9")
    supabase.refresh_response = auth_response("sess-9")

    session = service.refresh("refresh-token", DEVICE, "1.2.3.4")

    assert session.device_id == row["id"] and supabase.signed_out == []
    assert ("touch", row["id"], "1.2.3.4") in devices.calls  # last use shown on the Devices page


def test_refresh_of_an_otp_session_is_allowed(service, supabase, devices):
    devices.add(session_id="otp-9")
    supabase.refresh_response = auth_response("otp-9", methods=("otp",))

    assert service.refresh("refresh-token", DEVICE).access_token


@pytest.mark.parametrize("setup, info, code", [
    ("none", DEVICE, "device_session_required"),
    ("revoked", DEVICE, "device_revoked"),
    ("active", device_info(device_key=str(uuid.uuid4())), "device_session_required"),  # case 25: copied token
])
def test_case_07_25_refresh_refused_and_the_refreshed_session_signed_out(service, supabase, devices, setup, info, code):
    if setup != "none":
        devices.add(session_id="sess-9", revoked=setup == "revoked")
    supabase.refresh_response = auth_response("sess-9")

    _raises(lambda: service.refresh("refresh-token", info), 401, code)

    assert supabase.signed_out == [(supabase.refresh_response.session.access_token, "local")]


def test_refresh_of_a_recovery_session_is_still_refused(service, supabase, devices):
    devices.add(session_id="rec")
    supabase.refresh_response = auth_response("rec", methods=("recovery",))

    with pytest.raises(AdminError) as err:
        service.refresh("refresh-token", DEVICE)
    assert err.value.details == "INVALID_REFRESH_TOKEN"


# ---------- logout and the TEOS-307 hooks ----------

def _admin(device_id="dev-1", session_id="sess-1", token=None) -> AdminUser:
    from app.config.admin_permissions import permissions_for
    return AdminUser(id=ADMIN_ID, email=EMAIL, role=ROLE_SUPER_ADMIN, permissions=permissions_for(ROLE_SUPER_ADMIN),
                     token=token or session_token(session_id), session_id=session_id, device_id=device_id)


def test_logout_clears_the_device_session(service, supabase, devices):
    service.logout(_admin())

    assert ("clear_session", "sess-1") in devices.calls
    assert len(supabase.signed_out) == 1


def test_logout_succeeds_when_the_device_update_fails(service, devices):
    devices.fail = RuntimeError("db down")

    service.logout(_admin())


def test_case_21_change_password_revokes_other_devices_and_moves_the_caller(service, supabase, devices):
    supabase.password_response = auth_response("new-session")

    result = service.change_password(_admin(), "Old-Passw0rd!x", "N3w-Passw0rd!x", None,
                                     AuditContext(actor_id=ADMIN_ID))

    assert ("revoke_all", ADMIN_ID, REASON_PASSWORD_CHANGE, "dev-1") in devices.calls
    assert ("move_session", "dev-1", "new-session") in devices.calls
    assert ("record_token", "dev-1", 1000) in devices.calls  # the new session's token
    assert result.device_id == "dev-1"


def test_change_password_succeeds_when_the_device_update_fails(service, supabase, devices):
    devices.fail = RuntimeError("db down")

    result = service.change_password(_admin(), "Old-Passw0rd!x", "N3w-Passw0rd!x", None,
                                     AuditContext(actor_id=ADMIN_ID))

    assert result.access_token


def test_password_reset_revokes_every_device(service, supabase, devices, monkeypatch):
    supabase.auth.get_user = lambda jwt: SimpleNamespace(user=make_auth_user(ROLE_ADMIN, user_id=ADMIN_ID))

    service.complete_password_reset(session_token("link", "recovery"), "N3w-Passw0rd!x", None)

    assert ("revoke_all", ADMIN_ID, REASON_PASSWORD_RESET, None) in devices.calls


@pytest.mark.parametrize("methods", [("otp",), ("magiclink",)])
@pytest.mark.parametrize("revoked", [False, True])
def test_password_reset_with_a_portal_session_is_400(service, supabase, devices, methods, revoked):
    """A verify-device session (it has a device row, active or revoked) must not set a password without the
    current one."""
    supabase.auth.get_user = lambda jwt: SimpleNamespace(user=make_auth_user(ROLE_ADMIN, user_id=ADMIN_ID))
    updates = []
    supabase.auth.admin.update_user_by_id = lambda uid, attrs: updates.append(uid)
    devices.add(session_id="portal", revoked=revoked)

    with pytest.raises(AdminError) as err:
        service.complete_password_reset(session_token("portal", *methods), "N3w-Passw0rd!x", None)

    assert err.value.code == 400 and err.value.details == "reset_link_invalid"
    assert updates == []


@pytest.mark.parametrize("methods", [("recovery",), ("otp",), ("magiclink",)])
def test_password_reset_with_a_reset_link_session_without_a_row_works(service, supabase, devices, methods):
    supabase.auth.get_user = lambda jwt: SimpleNamespace(user=make_auth_user(ROLE_ADMIN, user_id=ADMIN_ID))

    assert service.complete_password_reset(session_token("link", *methods), "N3w-Passw0rd!x",
                                           None).sessions_signed_out is True


def test_password_reset_when_the_device_lookup_fails_is_502(service, supabase, devices):
    devices.fail, devices.fail_only = RuntimeError("db down"), {"session_device"}

    with pytest.raises(AdminError) as err:
        service.complete_password_reset(session_token("link", "recovery"), "N3w-Passw0rd!x", None)
    assert err.value.code == 502


def test_password_reset_succeeds_when_the_device_update_fails(service, supabase, devices):
    supabase.auth.get_user = lambda jwt: SimpleNamespace(user=make_auth_user(ROLE_ADMIN, user_id=ADMIN_ID))
    devices.fail, devices.fail_only = RuntimeError("db down"), {"revoke_all"}

    assert service.complete_password_reset(session_token("link", "recovery"), "N3w-Passw0rd!x",
                                           None).sessions_signed_out is True


# ---------- helpers ----------

@pytest.mark.parametrize("email, masked", [("john@montymobile.com", "j***@m***.com"),
                                           ("a@b.co.uk", "a***@b***.uk"), ("weird", "w***@***")])
def test_mask_email(email, masked):
    assert mask_email(email) == masked
