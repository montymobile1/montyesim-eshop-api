"""Phone sign-in OTPs are stored only as HMAC-SHA256(OTP_HMAC_SECRET, code).

Runs the real `AuthService` + `UserOtpService` against the in-memory GoTrue and `user_otp`
table of test_phone_login_no_static_password.py; no network, no database.
"""

import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from app.config import config
from app.config.constants import ErrorMessages, OtpChannelEnum
from app.exceptions import CustomException
from app.schemas.auth import LoginRequest, VerifyOtpRequest
from app.services.user_otp_service import UserOtpService, hash_otp
from test_phone_login_no_static_password import (  # noqa: F401 (fixtures)
    PHONE_A,
    PHONE_B,
    auth_service,
    factory,
    last_otp,
    login,
    otp_store,
    server,
    stubbed,
    synthetic_email,
    verify,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def rows_of(otp_store, phone):
    return [r for r in otp_store.rows if r["mobile"] == phone]


def assert_refused(exc_info, name, details):
    assert exc_info.value.code == 400
    assert exc_info.value.name == name
    assert exc_info.value.details == details


def both_login_type(key, default_value=None):
    values = {"OTP_EXPIRATION_TIME": 5, "login_type": "email_phone_both"}
    return values.get(str(getattr(key, "value", key)), default_value)


async def resend(service, phone=PHONE_A):
    with patch("app.services.auth_service.get_config", side_effect=both_login_type):
        return await service.resend_otp(LoginRequest(phone=phone, otp_channel=OtpChannelEnum.SMS))


def sent_code(service):
    return service._AuthService__dcb_service.send_otp.await_args.kwargs["otp"]


# --------------------------------------------------------------------------- storage

@pytest.mark.asyncio
async def test_valid_code_verifies(auth_service, otp_store):
    await login(auth_service, PHONE_A)

    response = await verify(auth_service, PHONE_A, last_otp(otp_store, PHONE_A))

    assert response.status == "success"
    assert response.data.access_token


@pytest.mark.asyncio
async def test_stored_value_is_a_64_char_hash_never_the_code(auth_service, otp_store):
    await login(auth_service, PHONE_A)
    code = last_otp(otp_store, PHONE_A)
    stored = rows_of(otp_store, PHONE_A)[-1]["otp"]

    assert re.fullmatch(r"[0-9a-f]{64}", stored)
    assert stored == hash_otp(code)
    assert code not in stored
    assert all(code not in str(value) for row in otp_store.rows for value in row.values())


def test_hash_is_keyed_with_the_secret(monkeypatch):
    first = hash_otp("123456")
    monkeypatch.setattr(config, "OTP_HMAC_SECRET", "another-secret")
    assert hash_otp("123456") != first


@pytest.mark.parametrize("drawn, expected", [(0, "000000"), (42, "000042"), (999999, "999999")])
def test_code_comes_from_secrets_and_keeps_leading_zeros(drawn, expected):
    with patch("app.services.user_otp_service.secrets.randbelow", return_value=drawn) as randbelow:
        assert UserOtpService._new_code() == expected
    randbelow.assert_called_once_with(10 ** 6)


def test_codes_are_always_six_digits():
    codes = [UserOtpService._new_code() for _ in range(2000)]
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert any(c.startswith("0") for c in codes)  # ~10% of draws; 2000 draws make a miss negligible


def test_generate_otp_uses_the_generator_seam():
    service = UserOtpService.__new__(UserOtpService)
    repo = service._UserOtpService__user_otp_repo = MagicMock()
    repo.list.return_value = []
    with patch.object(UserOtpService, "_new_code", return_value="012345") as new_code, \
            patch.object(service, "_UserOtpService__recent_otp_limit", return_value=False), \
            patch.object(service, "_UserOtpService__has_active_otp", return_value=False), \
            patch.object(service, "_UserOtpService__expire_other_active_otps"), \
            patch("app.services.user_otp_service.get_config", return_value="5"):
        assert service.generate_otp("+96170111111", "a@b.c") == "012345"
    new_code.assert_called_once()
    assert repo.create.call_args.args[0]["otp"] == hash_otp("012345")


# --------------------------------------------------------------------------- verify

@pytest.mark.asyncio
async def test_wrong_code_is_refused_as_today(auth_service, otp_store):
    await login(auth_service, PHONE_A)
    code = last_otp(otp_store, PHONE_A)
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"

    with pytest.raises(CustomException) as exc_info:
        await verify(auth_service, PHONE_A, wrong)

    assert_refused(exc_info, ErrorMessages.OTP_INVALID, "OTP Invalid")


@pytest.mark.asyncio
async def test_expired_code_is_refused_as_today(auth_service, server, otp_store):
    server.add_user(synthetic_email(PHONE_A), "x", {"msisdn": PHONE_A, "login_type": "phone"})
    otp_store.add(PHONE_A, synthetic_email(PHONE_A), "654321", minutes=-1)

    with pytest.raises(CustomException) as exc_info:
        await verify(auth_service, PHONE_A, "654321")

    assert_refused(exc_info, ErrorMessages.OTP_EXPIRED, "OTP Expired")


@pytest.mark.asyncio
async def test_code_of_phone_a_is_refused_for_phone_b(auth_service, otp_store):
    await login(auth_service, PHONE_A)
    await login(auth_service, PHONE_B)
    code_a = last_otp(otp_store, PHONE_A)
    if code_a == last_otp(otp_store, PHONE_B):  # 1 in a million: make the codes differ
        pytest.skip("both phones drew the same code")

    with pytest.raises(CustomException) as exc_info:
        await verify(auth_service, PHONE_B, code_a)

    assert_refused(exc_info, ErrorMessages.OTP_INVALID, "OTP Invalid")


@pytest.mark.asyncio
async def test_old_plaintext_row_cannot_verify(auth_service, server, otp_store):
    """A row written before the deploy holds the plain code; it must not verify afterwards."""
    server.add_user(synthetic_email(PHONE_A), "x", {"msisdn": PHONE_A, "login_type": "phone"})
    expire_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    otp_store.rows.append({"mobile": PHONE_A, "email": synthetic_email(PHONE_A), "otp": "123456",
                           "expire_at": expire_at, "is_used": False})

    with pytest.raises(CustomException) as exc_info:
        await verify(auth_service, PHONE_A, "123456")

    assert_refused(exc_info, ErrorMessages.OTP_INVALID, "OTP Invalid")


@pytest.mark.xfail(strict=True, reason="out of scope, follow-up: a used code is still accepted (replay)")
@pytest.mark.asyncio
async def test_used_code_is_refused(auth_service, otp_store):
    await login(auth_service, PHONE_A)
    code = last_otp(otp_store, PHONE_A)
    await verify(auth_service, PHONE_A, code)

    with pytest.raises(CustomException):
        await verify(auth_service, PHONE_A, code)


# --------------------------------------------------------------------------- resend

@pytest.mark.asyncio
async def test_resend_issues_a_new_code_and_refuses_the_previous_one(auth_service, otp_store):
    await login(auth_service, PHONE_A)
    previous = last_otp(otp_store, PHONE_A)

    await resend(auth_service)
    new = last_otp(otp_store, PHONE_A)

    assert sent_code(auth_service) == new
    assert rows_of(otp_store, PHONE_A)[-1]["otp"] == hash_otp(new)
    assert len(rows_of(otp_store, PHONE_A)) == 2
    if new != previous:  # 1 in a million they collide; the collision check re-draws only while active
        with pytest.raises(CustomException) as exc_info:
            await verify(auth_service, PHONE_A, previous)
        assert_refused(exc_info, ErrorMessages.OTP_EXPIRED, "OTP Expired")
    response = await verify(auth_service, PHONE_A, new)
    assert response.status == "success"


@pytest.mark.asyncio
async def test_resend_keeps_the_email_verify_matches_on(auth_service, otp_store):
    """The phone-only resend request has no email; the new code keeps the login's synthetic email."""
    await login(auth_service, PHONE_A)
    await resend(auth_service)

    assert rows_of(otp_store, PHONE_A)[-1]["email"] == synthetic_email(PHONE_A)


@pytest.mark.asyncio
async def test_three_sends_per_hour_still_enforced(auth_service, otp_store):
    await login(auth_service, PHONE_A)  # 1
    await resend(auth_service)  # 2
    await resend(auth_service)  # 3
    current = last_otp(otp_store, PHONE_A)

    with pytest.raises(CustomException) as exc_info:
        await resend(auth_service)  # 4th send in the hour
    assert exc_info.value.code == 400
    assert exc_info.value.name == ErrorMessages.OTP_LIMIT_REACHED
    with pytest.raises(CustomException) as exc_info:
        await login(auth_service, PHONE_A)
    assert exc_info.value.name == ErrorMessages.OTP_LIMIT_REACHED

    assert len(rows_of(otp_store, PHONE_A)) == 3
    response = await verify(auth_service, PHONE_A, current)  # a refused resend leaves the code working
    assert response.status == "success"


@pytest.mark.asyncio
async def test_login_while_a_code_is_active_is_still_429(auth_service):
    await login(auth_service, PHONE_A)

    with pytest.raises(CustomException) as exc_info:
        await login(auth_service, PHONE_A)

    assert exc_info.value.code == 429
    assert exc_info.value.name == ErrorMessages.OTP_STILL_ACTIVE


# --------------------------------------------------------------------------- metadata and logs

@pytest.mark.asyncio
async def test_new_user_metadata_has_no_otp(auth_service, server):
    await login(auth_service, PHONE_A)

    _, user = server.by_email(synthetic_email(PHONE_A))
    assert "otp" not in user["user_metadata"]
    assert user["user_metadata"]["msisdn"] == PHONE_A
    assert all("otp" not in call["options"]["data"] for call in server.calls["sign_up"])


@pytest.mark.asyncio
async def test_returning_user_metadata_update_has_no_otp(auth_service, server, otp_store):
    server.add_user(synthetic_email(PHONE_A), "x", {"msisdn": PHONE_A, "login_type": "phone"})

    await login(auth_service, PHONE_A)

    updates = [attrs["user_metadata"] for _, attrs in server.calls["update_user_by_id"] if "user_metadata" in attrs]
    assert updates == [{"msisdn": PHONE_A, "login_type": "phone"}]


@pytest.mark.asyncio
async def test_logs_of_login_and_verify_contain_no_code(auth_service, otp_store, caplog):
    captured = []
    sink = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    caplog.set_level(logging.DEBUG)
    try:
        await login(auth_service, PHONE_A)
        code = last_otp(otp_store, PHONE_A)
        await verify(auth_service, PHONE_A, code)
        with pytest.raises(CustomException):
            await verify(auth_service, PHONE_A, f"{(int(code) + 1) % 10 ** 6:06d}")
    finally:
        logger.remove(sink)

    logs = "\n".join(captured) + "\n" + caplog.text
    assert "verify_otp phone request received" in logs  # the request is still logged, masked
    assert code not in logs
    assert not re.search(r"(?<!\d)\d{6}(?!\d)", logs)


@pytest.mark.asyncio
async def test_email_verify_log_masks_the_pin(auth_service):
    captured = []
    sink = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    try:
        with pytest.raises(CustomException):
            await auth_service.verify_otp(VerifyOtpRequest(user_email="someone@example.com",
                                                           verification_pin="483920"), "device-1")
    finally:
        logger.remove(sink)

    logs = "\n".join(captured)
    assert "verify_otp email otp request received" in logs
    assert "483920" not in logs


# --------------------------------------------------------------------------- startup

def test_missing_secret_is_reported_by_startup_validation(monkeypatch):
    for value in (None, ""):
        monkeypatch.setattr(config, "OTP_HMAC_SECRET", value)
        with pytest.raises(EnvironmentError, match="OTP_HMAC_SECRET"):
            config.validate_required_env_vars()


@pytest.mark.parametrize("value", [None, ""])
def test_app_refuses_to_start_without_secret(value):
    env = {k: v for k, v in os.environ.items() if k != "OTP_HMAC_SECRET"}
    env["ENVIRONMENT"] = "otp-hash-test-no-such-env-file"  # load .env.<this>, which does not exist
    if value is not None:
        env["OTP_HMAC_SECRET"] = value
    result = subprocess.run([sys.executable, "-c", "import app.config.config"], cwd=PROJECT_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)

    assert result.returncode != 0
    assert "OTP_HMAC_SECRET" in result.stderr


def test_app_starts_with_secret():
    env = dict(os.environ, ENVIRONMENT="otp-hash-test-no-such-env-file", OTP_HMAC_SECRET="s")
    result = subprocess.run([sys.executable, "-c", "import app.config.config"], cwd=PROJECT_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr[-2000:]
