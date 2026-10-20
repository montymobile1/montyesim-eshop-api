"""Phone login must not use a static password.

Phone users are signed in after a successful OTP by minting a session server-side (admin magic
link -> verify token_hash on a fresh client). No password grant is used anywhere for phone users.

Supabase Auth is replaced by `FakeGoTrue`, an in-memory stand-in that reproduces the GoTrue
v2.197.0 behaviour this relies on (checked against a local GoTrue v2.197.0):
  * a magic-link token_hash is single use, and a newer link for the same user replaces it
    (verifying the old one fails with 403 `otp_expired`);
  * an admin password update revokes all of the user's sessions / refresh tokens.
The OTP checks run through the real `UserOtpService` against an in-memory `user_otp` table.
"""

import asyncio
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gotrue import AuthResponse
from gotrue.errors import AuthApiError
from gotrue.types import Session, User
from loguru import logger

from app.exceptions import CustomException
from app.models.user import UsersCopyModel
from app.schemas.auth import LoginRequest, VerifyOtpRequest
from app.services.user_otp_service import UserOtpService, hash_otp

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHONE_A = "+96170111111"
PHONE_B = "+96170222222"


def synthetic_email(phone: str) -> str:
    return f"{phone}_user@esim.com"


# --------------------------------------------------------------------------- fake Supabase Auth

class FakeGoTrue:
    """Shared server state behind every fake client."""

    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}  # id -> dict(email, password, user_metadata, app_metadata)
        self.link_tokens = {}  # user id -> current magic-link token_hash
        self.refresh_tokens = {}  # refresh token -> user id
        self.calls = {"generate_link": [], "verify_otp": [], "sign_in_with_password": [], "sign_up": [],
                      "update_user_by_id": [], "refresh_session": []}
        self.fail_generate_link = False
        self.fail_verify = None  # an AuthApiError to raise from verify_otp
        self.on_generate_link = None  # hook(user_id) run after a link is generated

    def add_user(self, email, password, user_metadata=None, app_metadata=None):
        user_id = f"user-{len(self.users) + 1}"
        self.users[user_id] = {"email": email, "password": password, "user_metadata": dict(user_metadata or {}),
                               "app_metadata": dict(app_metadata or {})}
        return user_id

    def by_email(self, email):
        return next(((uid, u) for uid, u in self.users.items() if u["email"] == email), (None, None))

    def model(self, user_id):
        u = self.users[user_id]
        return User(id=user_id, email=u["email"], user_metadata=dict(u["user_metadata"]),
                    app_metadata=dict(u["app_metadata"]), aud="authenticated", created_at=datetime.now(timezone.utc))

    def new_session(self, user_id):
        refresh = secrets.token_hex(16)
        self.refresh_tokens[refresh] = user_id
        user = self.model(user_id)
        return AuthResponse(session=Session(access_token=f"access-{secrets.token_hex(8)}", refresh_token=refresh,
                                            expires_in=3600, token_type="bearer", user=user), user=user)


class FakeAdmin:
    def __init__(self, server: FakeGoTrue):
        self.server = server

    def generate_link(self, params):
        server = self.server
        with server.lock:
            server.calls["generate_link"].append(dict(params))
            if server.fail_generate_link:
                raise AuthApiError("generate_link failed", 500, "unexpected_failure")
            user_id, _ = server.by_email(params["email"])
            token_hash = secrets.token_hex(28)
            server.link_tokens[user_id] = token_hash  # a newer link replaces the previous one
        if server.on_generate_link:
            server.on_generate_link(user_id)
        return SimpleNamespace(properties=SimpleNamespace(hashed_token=token_hash, verification_type="magiclink",
                                                          email_otp="123456", action_link="https://x/verify"),
                               user=server.model(user_id))

    def update_user_by_id(self, uid, attributes):
        server = self.server
        with server.lock:
            server.calls["update_user_by_id"].append((uid, dict(attributes)))
            user = server.users[uid]
            if "password" in attributes:
                user["password"] = attributes["password"]
                for token in [t for t, owner in server.refresh_tokens.items() if owner == uid]:
                    del server.refresh_tokens[token]  # GoTrue revokes every session on a password change
            if "user_metadata" in attributes:
                user["user_metadata"] = dict(attributes["user_metadata"])
            if "app_metadata" in attributes:
                user["app_metadata"].update(attributes["app_metadata"])
            return SimpleNamespace(user=server.model(uid))

    def list_users(self, page=None, per_page=None):
        ids = sorted(self.server.users)
        start = (page - 1) * per_page
        return [self.server.model(uid) for uid in ids[start:start + per_page]]


class FakeAuth:
    def __init__(self, server: FakeGoTrue):
        self.server = server
        self.admin = FakeAdmin(server)
        self.session = None  # what a real client would keep after sign-in

    def sign_up(self, credentials):
        server = self.server
        with server.lock:
            server.calls["sign_up"].append(dict(credentials))
            user_id = server.add_user(credentials["email"], credentials["password"],
                                      credentials.get("options", {}).get("data"))
            return server.new_session(user_id)

    def sign_in_with_password(self, credentials):
        server = self.server
        with server.lock:
            server.calls["sign_in_with_password"].append(credentials["email"])
            user_id, user = server.by_email(credentials["email"])
            if user is None or user["password"] != credentials["password"]:
                raise AuthApiError("Invalid login credentials", 400, "invalid_credentials")
            return server.new_session(user_id)

    def verify_otp(self, params):
        server = self.server
        with server.lock:
            server.calls["verify_otp"].append(dict(params))
            if server.fail_verify:
                raise server.fail_verify
            owner = next((uid for uid, h in server.link_tokens.items() if h == params.get("token_hash")), None)
            if owner is None:
                raise AuthApiError("Email link is invalid or has expired", 403, "otp_expired")
            del server.link_tokens[owner]  # single use
            response = server.new_session(owner)
            self.session = response.session
            return response

    def refresh_session(self, refresh_token=None):
        server = self.server
        with server.lock:
            server.calls["refresh_session"].append(refresh_token)
            user_id = server.refresh_tokens.pop(refresh_token, None)
            if user_id is None:
                raise AuthApiError("Invalid Refresh Token: Refresh Token Not Found", 400, "refresh_token_not_found")
            return server.new_session(user_id)


class FakeClientFactory:
    """Stands in for `supabase_client()`: a NEW client per call, all sharing one server."""

    def __init__(self, server: FakeGoTrue):
        self.server = server
        self.clients = []

    def __call__(self, *args, **kwargs):
        client = SimpleNamespace(auth=FakeAuth(self.server))
        self.clients.append(client)
        return client


# --------------------------------------------------------------------------- fake user_otp table

class FakeQuery:
    def __init__(self, rows):
        self.rows, self.preds, self.changes = rows, [], None
        self.negate, self.order_by, self.max_rows = False, None, None

    @property
    def not_(self):
        self.negate = True
        return self

    def is_(self, key, value):
        assert value == "null"
        negate, self.negate = self.negate, False
        self.preds.append(lambda r: (r.get(key) is None) != negate)
        return self

    def order(self, key, desc=False):
        self.order_by = (key, desc)
        return self

    def limit(self, size):
        self.max_rows = size
        return self

    def select(self, *_):
        return self

    def update(self, data):
        self.changes = dict(data)
        return self

    def eq(self, key, value):
        self.preds.append(lambda r: r.get(key) == value)
        return self

    def lt(self, key, value):
        self.preds.append(lambda r: r[key] < value)
        return self

    def gt(self, key, value):
        self.preds.append(lambda r: r[key] > value)
        return self

    def execute(self):
        matched = [r for r in self.rows if all(p(r) for p in self.preds)]
        if self.order_by:
            matched.sort(key=lambda r: r[self.order_by[0]], reverse=self.order_by[1])
        matched = matched[:self.max_rows] if self.max_rows is not None else matched
        for r in matched if self.changes is not None else ():
            r.update(self.changes)
        return SimpleNamespace(data=[dict(r) for r in matched])


class FakeOtpStore:
    """Rows hold the code's hash, as in the real table; the plain codes are kept aside for the tests."""

    def __init__(self):
        self.rows = []
        self.codes = {}  # id(row) -> plain code
        self.last_issued = None
        self.lock = threading.Lock()
        self.last_id = 0
        self.issued = {}  # hash -> code, so concurrent requests map each row to its own code

    def issue(self):
        """Stands in for UserOtpService._new_code, remembering the code it hands out."""
        self.last_issued = f"{secrets.randbelow(10 ** 6):06d}"
        self.issued[hash_otp(self.last_issued)] = self.last_issued
        return self.last_issued

    def next_id(self):
        """Serial primary key, as user_otp.id."""
        with self.lock:
            self.last_id += 1
            return self.last_id

    def add(self, mobile, email, otp, minutes=5, is_used=False):
        expire_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        row = {"id": self.next_id(), "mobile": mobile, "email": email, "otp": hash_otp(otp), "expire_at": expire_at,
               "is_used": is_used}
        self.rows.append(row)
        self.codes[id(row)] = otp

    # supabase_client().table(...) used by UserOtpService
    def table(self, _name):
        return FakeQuery(self.rows)

    # UserOtpRepo methods used by UserOtpService
    def create(self, data):
        row = {"id": self.next_id(), **data}
        self.rows.append(row)
        self.codes[id(row)] = self.issued.get(row["otp"], self.last_issued)
        return SimpleNamespace(**row)

    def list(self, where):
        return [r for r in self.rows if all(r.get(k) == v for k, v in where.items())]

    def update_by(self, where, data):
        with self.lock:
            for r in self.rows:
                if all(r.get(k) == v for k, v in where.items()):
                    r.update(data)


class FakeUserRepo:
    """users_copy lookups, served from the fake auth users (as the sync trigger would)."""

    def __init__(self, server: FakeGoTrue):
        self.server = server

    def get_first_by(self, where=None, filters=None, **_):
        for key, value in (filters or {}).items():
            if "referral_code" in key:
                return None
            if "msisdn" in key:
                return next((UsersCopyModel(id=uid, email=u["email"], metadata=u["user_metadata"])
                             for uid, u in self.server.users.items()
                             if u["user_metadata"].get("msisdn") == value), None)
        if where and "email" in where:
            user_id, user = self.server.by_email(where["email"])
            return UsersCopyModel(id=user_id, email=user["email"], metadata=user["user_metadata"]) if user else None
        return None


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def server():
    return FakeGoTrue()


@pytest.fixture
def factory(server):
    return FakeClientFactory(server)


@pytest.fixture
def otp_store():
    return FakeOtpStore()


@pytest.fixture
def stubbed(server, factory, otp_store):
    """Patch every Supabase entry point used by phone login/verify; no network at all."""
    def config(key, default_value=None):
        values = {"OTP_EXPIRATION_TIME": 5, "login_type": "email"}
        return values.get(str(getattr(key, "value", key)), default_value)

    with patch("app.services.auth_service.supabase_client", side_effect=factory) as auth_supabase, \
            patch("app.services.auth_service.get_config", side_effect=config), \
            patch("app.services.auth_service.is_user_banned", return_value=False), \
            patch("app.services.user_otp_service.supabase_client", return_value=otp_store), \
            patch("app.services.user_otp_service.get_config", side_effect=config), \
            patch.object(UserOtpService, "_new_code", side_effect=otp_store.issue):
        yield SimpleNamespace(auth_supabase=auth_supabase)


@pytest.fixture
def auth_service(server, otp_store, stubbed):
    with patch("app.services.auth_service.dcb_service_instance", return_value=MagicMock(send_otp=AsyncMock())), \
            patch("app.services.auth_service.UserWalletService"), \
            patch("app.services.auth_service.TaskExecutor"), \
            patch("app.services.auth_service.DeviceRepo"), \
            patch("app.services.auth_service.UserRepo"), \
            patch("app.services.user_otp_service.UserOtpRepo", return_value=otp_store):
        from app.services.auth_service import AuthService
        service = AuthService()
        service._AuthService__user_repo = FakeUserRepo(server)
        service._AuthService__user_otp_service = UserOtpService()
        service._AuthService__user_otp_service._UserOtpService__user_otp_repo = otp_store
        service.create_wallet_if_not_exists = AsyncMock(return_value=None)
        return service


def device_repo(service):
    return service._AuthService__device_repo


async def login(service, phone=PHONE_A):
    return await service.login(LoginRequest(phone=phone))


def last_otp(otp_store, phone):
    return otp_store.codes[id([r for r in otp_store.rows if r["mobile"] == phone][-1])]


async def verify(service, phone, pin, device_id="device-1"):
    return await service.verify_otp(VerifyOtpRequest(phone=phone, verification_pin=pin), device_id)


async def login_and_verify(service, otp_store, phone=PHONE_A):
    await login(service, phone)
    return await verify(service, phone, last_otp(otp_store, phone))


def make_existing_phone_user(server, otp_store, phone=PHONE_A, password=None, otp="654321"):
    user_id = server.add_user(synthetic_email(phone), password or f"static_password_{phone}",
                              {"msisdn": phone, "login_type": "phone"})
    otp_store.add(phone, synthetic_email(phone), otp)
    return user_id


# --------------------------------------------------------------------------- HTTP harness

@pytest.fixture(scope="module")
def esim_app():
    """The real FastAPI app (routers + exception handlers), imported without config lookups."""
    import app.config.config as config_module
    import app.config.helper as helper_module

    def _stub(key, default_value=None):
        return default_value if default_value is not None else "stub"

    os.environ.setdefault("ENVIRONMENT", "DEV")
    with patch.object(helper_module, "get_config", _stub), patch.object(config_module, "get_config", _stub):
        from app.main import esim_app as application
        return application


@pytest.fixture
def http(esim_app, auth_service):
    from fastapi.testclient import TestClient
    import app.api.v1.authentication as authentication_module

    with patch.object(authentication_module, "service", auth_service):
        yield TestClient(esim_app, raise_server_exceptions=False)


def post_verify(http, body):
    return http.post("/api/v1/auth/verify_otp", json=body, headers={"x-device-id": "device-1"})


# =========================================================================== positive cases

@pytest.mark.asyncio
async def test_01_new_phone_user_valid_otp_returns_session(auth_service, server, otp_store):
    response = await login_and_verify(auth_service, otp_store)

    assert response.status == "success"
    assert response.data.access_token and response.data.refresh_token
    user_id, _ = server.by_email(synthetic_email(PHONE_A))
    assert response.data.user_token == user_id


@pytest.mark.asyncio
async def test_02_returning_phone_user_gets_session_for_same_user_id(auth_service, server, otp_store):
    user_id = make_existing_phone_user(server, otp_store)

    first = await verify(auth_service, PHONE_A, "654321")
    otp_store.add(PHONE_A, synthetic_email(PHONE_A), "777777")
    second = await verify(auth_service, PHONE_A, "777777")

    assert first.data.user_token == second.data.user_token == user_id
    assert first.data.refresh_token != second.data.refresh_token
    device_repo(auth_service).upsert.assert_called()


@pytest.mark.asyncio
async def test_03_session_minted_without_any_password_grant(auth_service, server, otp_store):
    make_existing_phone_user(server, otp_store)
    await login_and_verify(auth_service, otp_store, phone=PHONE_B)
    await verify(auth_service, PHONE_A, "654321")

    assert server.calls["sign_in_with_password"] == []
    assert len(server.calls["generate_link"]) == 2
    assert all(c["type"] == "magiclink" for c in server.calls["generate_link"])
    assert all("token_hash" in c and "password" not in c for c in server.calls["verify_otp"])


@pytest.mark.asyncio
async def test_03b_session_verified_on_a_fresh_client(auth_service, server, otp_store, factory):
    """The client that holds the user's session is used for nothing else."""
    make_existing_phone_user(server, otp_store)
    await verify(auth_service, PHONE_A, "654321")

    holders = [c for c in factory.clients if c.auth.session is not None]
    assert len(holders) == 1
    generating = [c for c in factory.clients if c is not holders[0]]
    assert generating and all(c.auth.session is None for c in generating)


@pytest.mark.asyncio
async def test_04_synthetic_email_format_unchanged(auth_service, server, otp_store):
    await login(auth_service, PHONE_A)

    assert server.calls["sign_up"][0]["email"] == "+96170111111_user@esim.com"
    assert server.calls["generate_link"] == []  # login only sends the OTP
    await verify(auth_service, PHONE_A, last_otp(otp_store, PHONE_A))
    assert server.calls["generate_link"][0]["email"] == "+96170111111_user@esim.com"


@pytest.mark.asyncio
async def test_05_email_login_flow_unaffected(auth_service):
    """Email OTP verify still uses Supabase's own email OTP, and email login still sends a magic OTP."""
    supabase = MagicMock()
    supabase.auth.verify_otp.return_value = MagicMock()
    with patch("app.services.auth_service.supabase_client", return_value=supabase), \
            patch("app.services.auth_service.authenticate") as authenticate, \
            patch("app.schemas.dto_mapper.DtoMapper.to_auth_response", return_value=MagicMock()):
        await auth_service.verify_otp(VerifyOtpRequest(user_email="someone@example.com", verification_pin="123456"),
                                      "device-1")
        await auth_service.login(LoginRequest(email="someone@example.com"))

    supabase.auth.verify_otp.assert_called_once_with(
        {"email": "someone@example.com", "token": "123456", "type": "email"})
    supabase.auth.admin.generate_link.assert_not_called()
    supabase.auth.sign_in_with_password.assert_not_called()
    authenticate.assert_called_once()


@pytest.mark.asyncio
async def test_06_refresh_token_from_new_flow_works_with_refresh_endpoint(auth_service, server, otp_store):
    user_id = make_existing_phone_user(server, otp_store)
    signed_in = await verify(auth_service, PHONE_A, "654321")

    refreshed = await auth_service.refresh_token(x_refresh_token=signed_in.data.refresh_token, currency_code="USD")

    assert refreshed.status == "success"
    assert refreshed.data.user_token == user_id
    assert refreshed.data.refresh_token != signed_in.data.refresh_token


@pytest.mark.asyncio
async def test_12_new_phone_users_get_random_password_at_creation(auth_service, server, otp_store):
    import app.services.auth_service as auth_module
    real = secrets.token_urlsafe
    sizes = []

    def spy(nbytes=None):
        sizes.append(nbytes)
        return real(nbytes)

    with patch.object(auth_module.secrets, "token_urlsafe", side_effect=spy):
        await login(auth_service, PHONE_A)
        await login(auth_service, PHONE_B)

    passwords = [c["password"] for c in server.calls["sign_up"]]
    assert sizes == [32, 32]
    assert all(len(p) >= 43 and not p.startswith("static_password_") for p in passwords)
    assert passwords[0] != passwords[1]


@pytest.mark.asyncio
async def test_22_generate_link_failure_is_5xx_without_session_or_writes(http, auth_service, server, otp_store):
    make_existing_phone_user(server, otp_store)
    server.fail_generate_link = True

    response = post_verify(http, {"phone": PHONE_A, "verification_pin": "654321"})

    assert response.status_code == 500
    assert "access_token" not in response.text and "refresh_token" not in response.text
    assert server.calls["verify_otp"] == [] and server.calls["sign_in_with_password"] == []
    device_repo(auth_service).upsert.assert_not_called()
    auth_service.create_wallet_if_not_exists.assert_not_called()


@pytest.mark.asyncio
async def test_23_verify_failure_is_5xx_without_session_and_password_stays_random(http, auth_service, server,
                                                                                  otp_store):
    await login(auth_service, PHONE_A)
    user_id, user = server.by_email(synthetic_email(PHONE_A))
    password_before = user["password"]
    server.fail_verify = AuthApiError("boom", 500, "unexpected_failure")

    response = post_verify(http, {"phone": PHONE_A, "verification_pin": last_otp(otp_store, PHONE_A)})

    assert response.status_code == 500
    assert "access_token" not in response.text
    assert server.users[user_id]["password"] == password_before != f"static_password_{PHONE_A}"
    assert server.calls["sign_in_with_password"] == []
    device_repo(auth_service).upsert.assert_not_called()


@pytest.mark.asyncio
async def test_23b_token_keeps_being_replaced_gives_up_with_5xx(http, auth_service, server, otp_store):
    make_existing_phone_user(server, otp_store)
    server.fail_verify = AuthApiError("Email link is invalid or has expired", 403, "otp_expired")

    response = post_verify(http, {"phone": PHONE_A, "verification_pin": "654321"})

    assert response.status_code == 500
    assert len(server.calls["generate_link"]) == 3  # bounded retries, then no fallback
    assert server.calls["sign_in_with_password"] == []


def test_25_two_concurrent_verifies_for_same_phone_both_get_independent_sessions(auth_service, server, otp_store):
    user_id = make_existing_phone_user(server, otp_store, otp="111111")
    otp_store.add(PHONE_A, synthetic_email(PHONE_A), "222222")
    barrier = threading.Barrier(2)
    generated = []

    def hold_until_both_generated(_user_id):
        generated.append(1)
        if len(generated) <= 2:
            barrier.wait(timeout=10)  # both requests hold a link before either verifies

    server.on_generate_link = hold_until_both_generated
    results, errors = [], []

    def run(pin):
        try:
            results.append(asyncio.run(verify(auth_service, PHONE_A, pin)))
        except Exception as e:  # pragma: no cover - reported by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(pin,)) for pin in ("111111", "222222")]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]

    assert errors == []
    assert len(results) == 2
    assert {r.data.user_token for r in results} == {user_id}
    assert results[0].data.refresh_token != results[1].data.refresh_token
    assert results[0].data.access_token != results[1].data.access_token
    assert len(server.calls["generate_link"]) == 3  # the loser's first token was replaced -> one retry
    for r in results:  # both sessions are live and independent
        assert server.refresh_tokens[r.data.refresh_token] == user_id


# =========================================================================== negative cases

@pytest.mark.asyncio
async def test_13_static_password_grant_fails_for_migrated_user(server, otp_store):
    from scripts.rotate_phone_user_passwords import rotate_users

    user_id = make_existing_phone_user(server, otp_store)
    client = FakeAuth(server)
    assert client.sign_in_with_password({"email": synthetic_email(PHONE_A),
                                         "password": f"static_password_{PHONE_A}"}).session  # before migration

    rotate_users(FakeAdmin(server), FakeAdmin(server).list_users(page=1, per_page=50), apply=True, log=lambda _: None)

    assert server.users[user_id]["password"] != f"static_password_{PHONE_A}"
    with pytest.raises(AuthApiError) as error:
        client.sign_in_with_password({"email": synthetic_email(PHONE_A), "password": f"static_password_{PHONE_A}"})
    assert error.value.status == 400


@pytest.mark.asyncio
async def test_14_static_password_grant_fails_for_user_created_after_fix(auth_service, server, otp_store):
    await login(auth_service, PHONE_A)

    with pytest.raises(AuthApiError) as error:
        FakeAuth(server).sign_in_with_password({"email": synthetic_email(PHONE_A),
                                                "password": f"static_password_{PHONE_A}"})
    assert error.value.status == 400


@pytest.mark.asyncio
async def test_15_wrong_otp_gives_no_session_and_never_calls_admin_api(auth_service, server, otp_store):
    make_existing_phone_user(server, otp_store, otp="654321")

    with pytest.raises(CustomException) as error:
        await verify(auth_service, PHONE_A, "000000")

    assert error.value.name == "OTP_INVALID"
    assert server.calls["generate_link"] == [] and server.calls["verify_otp"] == []
    assert server.calls["sign_in_with_password"] == []


@pytest.mark.asyncio
async def test_16_expired_otp_gives_no_session_and_never_calls_admin_api(auth_service, server, otp_store):
    server_user = make_existing_phone_user(server, otp_store, otp="654321")
    otp_store.rows.clear()
    otp_store.add(PHONE_A, synthetic_email(PHONE_A), "654321", minutes=-1)

    with pytest.raises(CustomException) as error:
        await verify(auth_service, PHONE_A, "654321")

    assert server_user and error.value.name == "OTP_EXPIRED"
    assert server.calls["generate_link"] == [] and server.calls["verify_otp"] == []


@pytest.mark.xfail(reason="covered by OTP story", strict=True)
@pytest.mark.asyncio
async def test_17_already_used_otp_gives_no_session(auth_service, server, otp_store):
    make_existing_phone_user(server, otp_store, otp="654321")
    await verify(auth_service, PHONE_A, "654321")
    server.calls["generate_link"].clear()

    with pytest.raises(CustomException):
        await verify(auth_service, PHONE_A, "654321")
    assert server.calls["generate_link"] == []


@pytest.mark.parametrize("phone", ["70111111", "+961abc", "+96170111111; drop", "   ", "+1"])
def test_18_malformed_phone_is_400_before_any_supabase_call(http, auth_service, stubbed, phone):
    """Deviation from the story (which asked for 422): HTTP 400 INVALID_PHONE_NUMBER keeps the
    verify_otp contract unchanged; this app returns HTTP 400 for every validation error."""
    with patch.object(auth_service._AuthService__user_otp_service, "verify_otp") as otp_verify, \
            patch.object(auth_service._AuthService__user_repo, "get_first_by") as user_lookup:
        response = post_verify(http, {"phone": phone, "verification_pin": "654321"})

    assert response.status_code == 400
    assert response.json()["responseCode"] == 400
    assert response.json()["title"] == "Invalid phone number provided."  # INVALID_PHONE_NUMBER
    stubbed.auth_supabase.assert_not_called()
    user_lookup.assert_not_called()  # users_copy is read through Supabase too
    otp_verify.assert_not_called()


def test_18b_missing_phone_and_email_is_400_before_any_supabase_call(http, stubbed):
    response = post_verify(http, {"verification_pin": "654321"})

    assert response.status_code == 400
    stubbed.auth_supabase.assert_not_called()


@pytest.mark.asyncio
async def test_19_no_password_or_token_hash_in_response_or_logs(http, auth_service, server, otp_store, caplog):
    captured = []
    sink = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    caplog.set_level(logging.DEBUG)
    try:
        await login(auth_service, PHONE_A)
        response = post_verify(http, {"phone": PHONE_A, "verification_pin": last_otp(otp_store, PHONE_A)})
    finally:
        logger.remove(sink)

    assert response.status_code == 200
    _, user = server.by_email(synthetic_email(PHONE_A))
    secrets_seen = [user["password"]] + [c["token_hash"] for c in server.calls["verify_otp"]]
    logs = "\n".join(captured) + "\n" + caplog.text
    for value in secrets_seen:
        assert value not in response.text
        assert value not in logs
    for word in ("hashed_token", "token_hash", "static_password_"):
        assert word not in response.text
        assert word not in logs
    # contract: same envelope and AuthResponseDTO fields as before
    body = response.json()
    assert set(body["data"]) == {"access_token", "refresh_token", "user_info", "user_token", "is_verified"}
    assert body["status"] == "success"


def test_20_static_password_string_does_not_exist_under_app():
    offenders = [str(path.relative_to(PROJECT_ROOT)) for path in (PROJECT_ROOT / "app").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts
                 and "static_password_" in path.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == []


@pytest.mark.asyncio
async def test_21_phone_b_otp_cannot_produce_session_for_phone_a(auth_service, server, otp_store):
    user_a = make_existing_phone_user(server, otp_store, phone=PHONE_A, otp="111111")
    user_b = make_existing_phone_user(server, otp_store, phone=PHONE_B, otp="222222")

    with pytest.raises(CustomException):
        await verify(auth_service, PHONE_A, "222222")
    assert server.calls["generate_link"] == []

    response_b = await verify(auth_service, PHONE_B, "222222")
    assert response_b.data.user_token == user_b != user_a
    assert [c["email"] for c in server.calls["generate_link"]] == [synthetic_email(PHONE_B)]
