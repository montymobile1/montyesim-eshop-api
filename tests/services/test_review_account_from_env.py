"""The store-review account comes from REVIEW_ACCOUNT_EMAIL / _PIN / _PASSWORD instead of literals in code.

Runs the real AuthService against the in-memory GoTrue and user_otp table of
test_phone_login_no_static_password.py. The values below are deliberately not the old literals, to show that
they come from the environment.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger

from app.config.constants import ErrorMessages
from app.exceptions import CustomException
from app.schemas.auth import LoginRequest, VerifyOtpRequest
from test_phone_login_no_static_password import (  # noqa: F401 (fixtures)
    PHONE_A,
    auth_service,
    factory,
    otp_store,
    server,
    stubbed,
    synthetic_email,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REVIEW = {"REVIEW_ACCOUNT_EMAIL": "store.reviewer@example.com", "REVIEW_ACCOUNT_PIN": "424242",
          "REVIEW_ACCOUNT_PASSWORD": "Rev!ew-only-pass-7"}
EMAIL, PIN, PASSWORD = REVIEW.values()
REVIEW_PHONE = "+96170999999"
# A clean import of the app in a subprocess: other tests may leave unusable values in os.environ.
STARTUP_ENV = {"ENVIRONMENT": "review-account-test-no-such-env-file",  # loads .env.<this>, which does not exist
               "SUPABASE_URL": "https://dummy.supabase.co",
               "SUPABASE_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.dummy"}


@pytest.fixture
def review_env(monkeypatch):
    for name, value in REVIEW.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(params=[("REVIEW_ACCOUNT_EMAIL", None), ("REVIEW_ACCOUNT_PIN", None),
                        ("REVIEW_ACCOUNT_PASSWORD", None), ("REVIEW_ACCOUNT_PIN", ""),
                        ("REVIEW_ACCOUNT_PASSWORD", "")],
                ids=["no-email", "no-pin", "no-password", "empty-pin", "empty-password"])
def review_disabled(review_env, monkeypatch, request):
    name, value = request.param
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)


def login_type(value):
    def config(key, default_value=None):
        values = {"OTP_EXPIRATION_TIME": 5, "login_type": value}
        return values.get(str(getattr(key, "value", key)), default_value)
    return patch("app.services.auth_service.get_config", side_effect=config)


async def email_login(service, email=EMAIL):
    with patch("app.services.auth_service.authenticate") as authenticate:
        response = await service.login(LoginRequest(email=email))
    return response, authenticate


async def phone_login(service, email=EMAIL, phone=REVIEW_PHONE):
    with login_type("phone"):
        return await service.login(LoginRequest(email=email, phone=phone))


async def email_verify(service, pin=PIN, email=EMAIL):
    return await service.verify_otp(VerifyOtpRequest(user_email=email, verification_pin=pin), "device-1")


async def phone_verify(service, pin=PIN, email=EMAIL, phone=REVIEW_PHONE):
    return await service.verify_otp(VerifyOtpRequest(phone=phone, user_email=email, verification_pin=pin), "device-1")


def sms(service):
    return service._AuthService__dcb_service.send_otp


# --------------------------------------------------------------------------- all three set: as before

@pytest.mark.asyncio
async def test_email_login_signs_the_review_account_up_once_with_the_env_password(review_env, auth_service, server):
    response, authenticate = await email_login(auth_service)
    assert response.status == "success"
    assert server.calls["sign_up"] == [{"email": EMAIL, "password": PASSWORD}]
    authenticate.assert_not_called()  # no email OTP

    server.users[server.by_email(EMAIL)[0]]["password"] = "changed"
    with patch.object(auth_service._AuthService__user_repo, "get_first_by",
                      return_value=server.model(server.by_email(EMAIL)[0])):
        await email_login(auth_service)
    assert len(server.calls["sign_up"]) == 1  # existing account: nothing created or updated


@pytest.mark.asyncio
async def test_phone_login_creates_or_resets_the_review_account_without_otp(review_env, auth_service, server,
                                                                            otp_store):
    response = await phone_login(auth_service)
    assert response.status == "success"
    assert server.calls["sign_up"] == [{"email": EMAIL, "password": PASSWORD}]

    user_id, user = server.by_email(EMAIL)
    user["password"] = "changed"
    await phone_login(auth_service)
    assert server.calls["update_user_by_id"] == [(user_id, {"password": PASSWORD})]
    assert user["password"] == PASSWORD

    assert otp_store.rows == []
    sms(auth_service).assert_not_called()


@pytest.mark.asyncio
async def test_phone_login_bypass_still_needs_a_phone_login_type(review_env, auth_service, server, otp_store):
    with login_type("email"):
        await auth_service.login(LoginRequest(email=EMAIL, phone=REVIEW_PHONE))
    assert [r["mobile"] for r in otp_store.rows] == [REVIEW_PHONE]  # normal OTP flow, as before
    assert all(call["password"] != PASSWORD for call in server.calls["sign_up"])


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [email_verify, phone_verify], ids=["email-verify", "phone-verify"])
async def test_verify_with_the_review_pin_signs_in_with_the_env_password(review_env, auth_service, server, otp_store,
                                                                         verify):
    server.add_user(EMAIL, PASSWORD)
    response = await verify(auth_service)
    assert response.status == "success"
    assert response.data.access_token
    assert server.calls["sign_in_with_password"] == [EMAIL]
    assert server.calls["verify_otp"] == []  # no OTP check


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [email_verify, phone_verify], ids=["email-verify", "phone-verify"])
async def test_verify_with_another_pin_is_not_the_bypass(review_env, auth_service, server, verify):
    server.add_user(EMAIL, PASSWORD)
    with pytest.raises(CustomException):
        await verify(auth_service, pin="123123")  # the old literal pin is not special any more
    assert server.calls["sign_in_with_password"] == []


# --------------------------------------------------------------------------- any of them unset: no bypass

@pytest.mark.asyncio
async def test_disabled_email_login_goes_through_normal_email_otp(review_disabled, auth_service, server):
    response, authenticate = await email_login(auth_service)
    assert response.status == "success"
    authenticate.assert_called_once()  # the normal email OTP request
    assert server.calls["sign_up"] == []


@pytest.mark.asyncio
async def test_disabled_phone_login_needs_a_normal_otp(review_disabled, auth_service, server, otp_store):
    await phone_login(auth_service)
    assert [r["mobile"] for r in otp_store.rows] == [REVIEW_PHONE]
    sms(auth_service).assert_awaited_once()
    assert server.calls["update_user_by_id"] == []
    assert all(call["password"] != PASSWORD for call in server.calls["sign_up"])  # normal sign-up, random password


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [email_verify, phone_verify], ids=["email-verify", "phone-verify"])
async def test_disabled_review_pin_is_refused_like_any_wrong_code(review_disabled, auth_service, server, verify):
    server.add_user(EMAIL, PASSWORD, {"msisdn": REVIEW_PHONE, "login_type": "phone"})
    with pytest.raises(CustomException) as exc_info:
        await verify(auth_service)
    assert exc_info.value.code == 400
    assert exc_info.value.name in (ErrorMessages.VERIFY_FAILED, ErrorMessages.OTP_INVALID)
    assert server.calls["sign_in_with_password"] == []
    assert server.calls["sign_up"] == [] and server.calls["update_user_by_id"] == []


def test_startup_warns_once_and_still_starts_when_unset():
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("REVIEW_ACCOUNT_")}, **STARTUP_ENV}
    result = subprocess.run([sys.executable, "-c", "import app.services.auth_service"], cwd=PROJECT_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stderr.count("store-review account disabled") == 1


def test_startup_does_not_warn_when_set():
    env = {**os.environ, **STARTUP_ENV, **REVIEW}
    result = subprocess.run([sys.executable, "-c", "import app.services.auth_service"], cwd=PROJECT_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "store-review account disabled" not in result.stderr
    assert all(value not in result.stderr for value in REVIEW.values())


# --------------------------------------------------------------------------- normal users unaffected

@pytest.mark.asyncio
async def test_normal_email_login_unaffected(review_env, auth_service, server):
    response, authenticate = await email_login(auth_service, email="someone@example.com")
    assert response.status == "success"
    authenticate.assert_called_once()
    assert server.calls["sign_up"] == []


@pytest.mark.asyncio
async def test_normal_phone_login_and_verify_unaffected(review_env, auth_service, otp_store):
    with login_type("phone"):
        await auth_service.login(LoginRequest(phone=PHONE_A))
    sms(auth_service).assert_awaited_once()
    code = otp_store.codes[id(otp_store.rows[-1])]
    response = await auth_service.verify_otp(VerifyOtpRequest(phone=PHONE_A, verification_pin=code), "device-1")
    assert response.status == "success"
    with pytest.raises(CustomException):  # the review pin means nothing for another user
        await phone_verify(auth_service, email=None, phone=PHONE_A)


# --------------------------------------------------------------------------- no values in code, logs, responses

def test_old_literals_no_longer_appear_in_app():
    old = {"test.apple@example.com", "esim_oss@2025", "123123"}
    found = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        text = path.read_text()
        found += [f"{path}: {v}" for v in old - {"123123"} if v in text]
        # the pin as a value; "961123123" (default WhatsApp number) is not the pin
        found += [f"{path}:{node.lineno}: 123123" for node in ast.walk(ast.parse(text))
                  if isinstance(node, ast.Constant) and node.value in ("123123", 123123)]
    assert found == []


@pytest.mark.asyncio
async def test_pin_and_password_never_in_logs_or_responses(review_env, auth_service, server):
    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        responses = [(await email_login(auth_service))[0], await phone_login(auth_service),
                     await email_verify(auth_service), await phone_verify(auth_service)]
    finally:
        logger.remove(sink)
    text = "\n".join(lines) + "\n".join(r.model_dump_json() for r in responses)
    assert PIN not in text
    assert PASSWORD not in text
