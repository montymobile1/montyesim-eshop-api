"""Only the latest phone sign-in code verifies (QA: code #1 still logged in after code #2 was sent).

Every path that issues a phone code ends in UserOtpService.generate_otp: login (all login types),
resend for login_type email_phone_both (reissue_otp) and resend for the other login types (= login).
Sequentially, a second code is only issued once the first is expired: login and the non-"both"
resend refuse with 429 OTP_STILL_ACTIVE while a code is active, and reissue_otp expires it first.
Two requests racing past the active-code check (double tap, login + resend at once, a client
retry) each stored a code and both verified. Now generate_otp expires every other active code
of the mobile once its own code is stored, keeping only the latest (highest id).

Runs the real AuthService + UserOtpService against the in-memory GoTrue and user_otp table of
test_phone_login_no_static_password.py.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config.constants import ErrorMessages, OtpChannelEnum
from app.exceptions import CustomException
from app.schemas.auth import LoginRequest
from test_phone_login_no_static_password import (  # noqa: F401 (fixtures)
    PHONE_A,
    PHONE_B,
    auth_service,
    factory,
    otp_store,
    server,
    stubbed,
    synthetic_email,
    verify,
)

PHONE_LOGIN_TYPES = ["phone", "email_phone", "email_phone_both"]


def config_for(login_type):
    def get(key, default_value=None):
        values = {"OTP_EXPIRATION_TIME": 5, "login_type": login_type}
        return values.get(str(getattr(key, "value", key)), default_value)
    return get


async def request_code(service, endpoint, login_type, phone=PHONE_A):
    """POST /auth/login or /auth/resend-otp for a phone, SMS channel."""
    with patch("app.services.auth_service.get_config", side_effect=config_for(login_type)):
        fn = service.login if endpoint == "login" else service.resend_otp
        return await fn(LoginRequest(phone=phone, otp_channel=OtpChannelEnum.SMS))


def sent_codes(service):
    return [call.kwargs["otp"] for call in service._AuthService__dcb_service.send_otp.await_args_list]


def expire_all(otp_store):
    for row in otp_store.rows:
        row["expire_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


async def assert_refused_as_expired(service, code, phone=PHONE_A):
    with pytest.raises(CustomException) as exc_info:
        await verify(service, phone, code)
    assert exc_info.value.code == 400
    assert exc_info.value.name == ErrorMessages.OTP_EXPIRED
    assert exc_info.value.details == "OTP Expired"


async def assert_logs_in(service, code, phone=PHONE_A):
    response = await verify(service, phone, code)
    assert response.status == "success"
    assert response.data.access_token


def race(service, requests):
    """Runs the requests at once; all pass the active-code check before any of them stores its code."""
    otp_service = service._AuthService__user_otp_service
    barrier = threading.Barrier(len(requests))
    has_active = otp_service._UserOtpService__has_active_otp
    errors = []

    def checked_together(mobile):
        result = has_active(mobile)
        barrier.wait(timeout=5)
        return result

    def run(endpoint, login_type):
        try:
            asyncio.run(request_code(service, endpoint, login_type))
        except Exception as e:  # pragma: no cover - surfaced by the assertion below
            errors.append(e)

    with patch.object(otp_service, "_UserOtpService__has_active_otp", side_effect=checked_together):
        threads = [threading.Thread(target=run, args=request) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert errors == []


def code_of_latest_row(service, otp_store, phone=PHONE_A):
    """The SMS code stored in the row with the highest id (send order between threads is not defined)."""
    latest = max((r for r in otp_store.rows if r["mobile"] == phone), key=lambda r: r["id"])
    return next(c for c in sent_codes(service) if otp_store.codes[id(latest)] == c)


# --------------------------------------------------------------------------- the QA scenario

@pytest.mark.parametrize("first, requests", [
    pytest.param(None, [("login", "phone"), ("login", "phone")], id="double-login-phone"),
    pytest.param(None, [("login", "email_phone"), ("resend", "email_phone")], id="login+resend-email_phone"),
    pytest.param("email_phone_both", [("resend", "email_phone_both"), ("resend", "email_phone_both")],
                 id="double-resend-both"),
])
@pytest.mark.asyncio
async def test_qa_two_different_codes_only_the_latest_verifies(auth_service, otp_store, first, requests):
    """QA: code #1 sent, code #2 (different) sent, code #1 must be refused and code #2 must work."""
    if first:
        await request_code(auth_service, "login", first)
        auth_service._AuthService__dcb_service.send_otp.reset_mock()
    race(auth_service, requests)
    codes = sent_codes(auth_service)
    assert len(codes) == 2
    if codes[0] == codes[1]:  # 1 in a million
        pytest.skip("both requests drew the same code")
    latest = code_of_latest_row(auth_service, otp_store)
    older = next(c for c in codes if c != latest)

    await assert_refused_as_expired(auth_service, older)
    await assert_logs_in(auth_service, latest)


@pytest.mark.asyncio
async def test_a_code_stored_out_of_order_still_leaves_only_the_highest_id(auth_service, otp_store):
    """The request with the lower id stores its row after the other request already expired older
    codes: it sees the newer code and expires its own."""
    other = {}
    create = otp_store.create

    def create_then_newer_code_appears(data):
        created = create(data)
        if not other:
            other["code"] = "111111" if otp_store.last_issued != "111111" else "222222"
            otp_store.add(PHONE_A, synthetic_email(PHONE_A), other["code"])  # higher id, still active
        return created

    with patch.object(otp_store, "create", side_effect=create_then_newer_code_appears):
        await request_code(auth_service, "login", "phone")
    own = sent_codes(auth_service)[0]

    await assert_refused_as_expired(auth_service, own)
    await assert_logs_in(auth_service, other["code"])


# --------------------------------------------------------------------------- each issuing path

@pytest.mark.parametrize("login_type", PHONE_LOGIN_TYPES)
@pytest.mark.asyncio
async def test_login_after_the_code_expired_only_the_new_code_verifies(auth_service, otp_store, login_type):
    await request_code(auth_service, "login", login_type)
    expire_all(otp_store)
    await request_code(auth_service, "login", login_type)
    first, latest = sent_codes(auth_service)

    if first != latest:
        await assert_refused_as_expired(auth_service, first)
    await assert_logs_in(auth_service, latest)


@pytest.mark.asyncio
async def test_resend_email_phone_both_only_the_new_code_verifies(auth_service):
    await request_code(auth_service, "login", "email_phone_both")
    await request_code(auth_service, "resend", "email_phone_both")
    first, latest = sent_codes(auth_service)

    if first != latest:
        await assert_refused_as_expired(auth_service, first)
    await assert_logs_in(auth_service, latest)


@pytest.mark.asyncio
async def test_resend_email_phone_both_after_the_code_expired_only_the_new_code_verifies(auth_service, otp_store):
    """With no active code to replace, the resent code keeps the email of the mobile's latest code
    (it was stored without one, so it could never verify: OTP Invalid)."""
    await request_code(auth_service, "login", "email_phone_both")
    expire_all(otp_store)
    await request_code(auth_service, "resend", "email_phone_both")
    first, latest = sent_codes(auth_service)

    assert otp_store.rows[-1]["email"] == synthetic_email(PHONE_A)
    if first != latest:
        await assert_refused_as_expired(auth_service, first)
    await assert_logs_in(auth_service, latest)


@pytest.mark.parametrize("login_type", ["phone", "email_phone"])
@pytest.mark.asyncio
async def test_resend_other_login_types_unchanged_while_active_then_only_the_new_code(auth_service, otp_store,
                                                                                      login_type):
    await request_code(auth_service, "login", login_type)
    with pytest.raises(CustomException) as exc_info:  # unchanged: resend = login, refused while active
        await request_code(auth_service, "resend", login_type)
    assert exc_info.value.code == 429
    assert exc_info.value.name == ErrorMessages.OTP_STILL_ACTIVE

    expire_all(otp_store)
    await request_code(auth_service, "resend", login_type)
    first, latest = sent_codes(auth_service)
    if first != latest:
        await assert_refused_as_expired(auth_service, first)
    await assert_logs_in(auth_service, latest)


@pytest.mark.asyncio
async def test_collision_retry_issues_one_code_and_expires_the_rest(auth_service, otp_store):
    """A drawn code whose hash matches an unused row of the mobile is re-drawn; the row stored is the
    only active one."""
    otp_store.add(PHONE_A, synthetic_email(PHONE_A), "123456", minutes=-1)  # unused, expired
    draws = iter(["123456", "654321"])

    def draw():
        otp_store.last_issued = next(draws)
        return otp_store.last_issued

    with patch("app.services.user_otp_service.UserOtpService._new_code", side_effect=draw):
        await request_code(auth_service, "login", "phone")

    assert sent_codes(auth_service) == ["654321"]
    now = datetime.now(timezone.utc).isoformat()
    active = [r for r in otp_store.rows if r["mobile"] == PHONE_A and not r["is_used"] and r["expire_at"] > now]
    assert len(active) == 1


@pytest.mark.asyncio
async def test_collision_retry_keeps_the_email_so_the_code_verifies(auth_service, otp_store):
    """The re-drawn code was stored without the email verify matches on, so it never verified."""
    otp_store.add(PHONE_A, synthetic_email(PHONE_A), "123456", minutes=-1)  # unused, expired
    draws = iter(["123456", "654321"])

    def draw():
        otp_store.last_issued = next(draws)
        return otp_store.last_issued

    with patch("app.services.user_otp_service.UserOtpService._new_code", side_effect=draw):
        await request_code(auth_service, "login", "phone")

    assert otp_store.rows[-1]["email"] == synthetic_email(PHONE_A)
    await assert_logs_in(auth_service, "654321")


# --------------------------------------------------------------------------- what must not change

@pytest.mark.asyncio
async def test_other_phones_codes_are_untouched(auth_service):
    await request_code(auth_service, "login", "phone", phone=PHONE_A)
    await request_code(auth_service, "login", "phone", phone=PHONE_B)
    code_a, code_b = sent_codes(auth_service)

    await assert_logs_in(auth_service, code_a, phone=PHONE_A)
    await assert_logs_in(auth_service, code_b, phone=PHONE_B)


@pytest.mark.asyncio
async def test_codes_expired_by_a_newer_code_still_count_towards_the_hourly_limit(auth_service, otp_store):
    race(auth_service, [("login", "phone"), ("login", "phone")])  # sends 1 and 2, one expired by the other
    expire_all(otp_store)
    await request_code(auth_service, "login", "phone")  # send 3

    with pytest.raises(CustomException) as exc_info:
        await request_code(auth_service, "login", "phone")
    assert exc_info.value.code == 400
    assert exc_info.value.name == ErrorMessages.OTP_LIMIT_REACHED
