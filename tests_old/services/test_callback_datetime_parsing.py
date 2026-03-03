import pytest
from app.config.utils import parse_iso_datetime


def test_parse_iso_with_fractional_and_tz():
    s = "2023-01-01T12:34:56.749657+00:00"
    dt = parse_iso_datetime(s)
    assert dt is not None
    assert dt.year == 2023
    assert dt.month == 1
    assert dt.day == 1
    assert dt.hour == 12
    assert dt.minute == 34
    assert dt.second == 56
    assert dt.microsecond == 749657
    assert dt.tzinfo is not None


def test_parse_invalid_returns_none():
    assert parse_iso_datetime("") is None
    assert parse_iso_datetime(None) is None
    assert parse_iso_datetime("not a date") is None
