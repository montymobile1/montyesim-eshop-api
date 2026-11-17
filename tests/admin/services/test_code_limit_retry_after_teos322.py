"""TEOS-322: 429 ``code_limit`` answers ``retry_after`` = the seconds until the hourly limit lets a new code through
(when the code that keeps the admin at the limit turns 1 hour old), not the constant 3600.

The limit itself is unchanged: same rows, same count (a verification plus its resends, at the verification's
``created_at``). Only the number in the answer changes, and the limit lifts exactly when that number says.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.services.admin.device_verification_service import (HOUR_SECONDS, MAX_CODES_PER_HOUR,
                                                             code_limit_retry_after)
from tests.admin.services.test_trusted_devices_signin import (ADMIN_ID, DEVICE, EMAIL, T0, _login, _raises,
                                                             device_info)
from tests.admin.services.test_trusted_devices_signin import (  # noqa: F401  (fixtures)
    audit, clock, devices, otp_repo, service, supabase, verification_repo, verifications)
from tests.admin.services.test_trusted_devices_manage import db_table  # noqa: F401  (fixture)


def _five_logins_at(service, clock, offsets):
    """One login (one code) at each offset in seconds from T0, each from a new browser as in QA's steps."""
    for index, offset in enumerate(offsets):
        clock.now = T0 + timedelta(seconds=offset)
        _login(service, info=device_info(browser=f"Browser{index}"))


# ---------- QA's steps ----------

def test_qa_step_1_sixth_code_54_minutes_before_the_limit_lifts(service, supabase, clock):
    """Oldest code 13:16:44, 6th request 13:22:27: about 3257 s, not 3600."""
    _five_logins_at(service, clock, [0, 60, 120, 240, 300])
    clock.now = T0 + timedelta(seconds=343)

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit", retry_after=3600 - 343)
    assert len(supabase.sent) == 5  # no email for the 6th


def test_qa_step_2_thirteen_seconds_before_the_limit_lifts(service, clock):
    _five_logins_at(service, clock, [0, 60, 120, 240, 300])
    clock.now = T0 + timedelta(seconds=3600 - 13)

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit", retry_after=13)


def test_qa_step_3_the_limit_lifts_exactly_when_retry_after_says(service, supabase, clock):
    """The number is the real wait: one second earlier is still refused, at the moment the code goes out."""
    _five_logins_at(service, clock, [0, 60, 120, 240, 300])
    clock.now = T0 + timedelta(seconds=1000)
    wait = _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit")["retry_after"]
    assert wait == 2600

    clock.advance(wait - 1)
    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit", retry_after=1)
    clock.advance(1.001)  # the window keeps a code sent exactly 1 hour ago (created_at >= now - 1 h, unchanged)
    _login(service)
    assert len(supabase.sent) == 6


def test_after_the_oldest_leaves_the_next_oldest_sets_the_wait(service, clock):
    _five_logins_at(service, clock, [0, 60, 120, 240, 300])
    clock.now = T0 + timedelta(seconds=3601)
    _login(service, info=device_info(browser="Sixth"))  # the code of T0 left the hour

    clock.now = T0 + timedelta(seconds=3602)  # the next oldest (T0 + 60 s) leaves at T0 + 3660 s
    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit", retry_after=58)


# ---------- resends ----------

def test_resend_refused_by_the_hourly_limit_gets_the_real_wait(service, clock):
    """Resends count at their verification's created_at (the limit's rule, unchanged)."""
    _five_logins_at(service, clock, [0, 120])
    verification = _login(service, info=device_info(browser="Third"))  # at T0 + 120 s too
    clock.advance(61)
    service.resend_code(verification.verification_id, None)
    clock.advance(61)
    service.resend_code(verification.verification_id, None)  # 5 codes: T0, then 4 at T0 + 120 s
    clock.advance(61)

    _raises(lambda: service.resend_code(verification.verification_id, None), 429, "code_limit",
            retry_after=3600 - 303)  # the oldest code (T0); now T0 + 120 + 3 x 61 s


def test_login_refused_after_resends_counts_them(service, clock):
    verification = _login(service)
    for _ in range(3):
        clock.advance(60)
        service.resend_code(verification.verification_id, None)  # 4 codes, all counted at T0
    clock.advance(1)
    _login(service)  # 5th, at T0 + 181 s

    _raises(lambda: service.login(EMAIL, "secret", None, DEVICE), 429, "code_limit", retry_after=3600 - 181)


# ---------- the calculation ----------

def _times(*offsets):
    return [(T0 + timedelta(seconds=o)).isoformat() for o in offsets]


@pytest.mark.parametrize("offsets, now, expected", [
    ((0, 10, 20, 30, 40), 100, 3500),               # exactly at the limit: the oldest
    ((0, 0, 0, 0, 0), 3599.2, 1),                    # rounded up, never 0
    ((0, 10, 20, 30, 40), 3600, 1),                  # at the boundary: minimum 1
    ((0, 10, 20, 30, 40, 50), 100, 3510),            # over the limit (two requests at once): position len - 5
    ((0, 0, 0, 0, 0, 0, 900), 1000, 2600),           # a verification with resends: same time, several codes
])
def test_code_limit_retry_after(offsets, now, expected):
    assert code_limit_retry_after(_times(*offsets), T0 + timedelta(seconds=now)) == expected


def test_code_limit_retry_after_accepts_datetimes_and_z_suffix():
    times = [T0 + timedelta(seconds=10)] + [(T0 + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")] * 4
    assert code_limit_retry_after(times, T0 + timedelta(seconds=100)) == 3510


@pytest.mark.parametrize("bad", [None, "not-a-date"])
def test_code_limit_retry_after_unreadable_time_keeps_the_old_upper_bound(bad):
    times = [bad] + _times(10, 20, 30, 40)
    assert code_limit_retry_after(times, T0) == HOUR_SECONDS


def test_code_limit_retry_after_below_the_limit_is_1():
    assert code_limit_retry_after(_times(*range(MAX_CODES_PER_HOUR - 1)), T0) == 1


# ---------- the repo ----------

def test_repo_send_times_one_per_code_oldest_first(db_table):
    from app.repo.admin.trusted_device_repo import AdminDeviceVerificationRepo
    chain = db_table.select.return_value.eq.return_value.gte.return_value.order.return_value
    chain.execute.return_value = SimpleNamespace(data=[
        {"created_at": "2026-11-05T10:00:00+00:00", "resend_count": 2},
        {"created_at": "2026-11-05T10:05:00+00:00", "resend_count": None},
    ])
    since = T0 - timedelta(hours=1)

    times = AdminDeviceVerificationRepo().send_times_since(ADMIN_ID, since)

    assert times == ["2026-11-05T10:00:00+00:00"] * 3 + ["2026-11-05T10:05:00+00:00"]
    db_table.select.assert_called_once_with("created_at,resend_count")
    db_table.select.return_value.eq.assert_called_once_with("admin_id", ADMIN_ID)
    assert db_table.select.return_value.eq.return_value.gte.call_args.args[0] == "created_at"
    db_table.select.return_value.eq.return_value.gte.return_value.order.assert_called_once_with("created_at")
