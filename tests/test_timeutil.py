"""Timestamp parsing and Shanghai calendar rules."""

import pytest

from app import errors
from app.timeutil import (
    Interval,
    TimeParseError,
    parse_timestamp,
    to_iso,
    validate_booking_window,
)


def test_shanghai_offset_accepted():
    dt = parse_timestamp("2026-09-09T09:00:00+08:00")
    assert not isinstance(dt, TimeParseError)
    assert to_iso(dt) == "2026-09-09T09:00:00+08:00"
    assert dt.utcoffset().total_seconds() == 8 * 3600


@pytest.mark.parametrize(
    "text,code",
    [
        ("2026-09-09T09:00:00", errors.BAD_TIMESTAMP),  # naive
        ("2026-09-09T09:00:00Z", errors.TIMEZONE_MISMATCH),
        ("2026-09-09T09:00:00+00:00", errors.TIMEZONE_MISMATCH),
        ("2026-09-09T10:00:00+09:00", errors.TIMEZONE_MISMATCH),
        ("not-a-timestamp", errors.BAD_TIMESTAMP),
        ("", errors.BAD_TIMESTAMP),
        (123, errors.BAD_TYPE),
        (None, errors.BAD_TYPE),
    ],
)
def test_bad_timestamps(text, code):
    outcome = parse_timestamp(text, field="starts_at")
    assert isinstance(outcome, TimeParseError)
    assert outcome.code == code


def test_end_after_start_ok():
    start, end = validate_booking_window(
        "2026-09-09T09:00:00+08:00", "2026-09-09T10:30:00+08:00"
    )
    assert end > start


def test_end_not_after_start():
    err, second = validate_booking_window(
        "2026-09-09T10:00:00+08:00", "2026-09-09T10:00:00+08:00"
    )
    assert second is None
    assert isinstance(err, TimeParseError)
    assert err.code == errors.END_NOT_AFTER_START


def test_cross_midnight_rejected():
    err, _ = validate_booking_window(
        "2026-09-09T23:30:00+08:00", "2026-09-10T00:30:00+08:00"
    )
    assert isinstance(err, TimeParseError)
    assert err.code == errors.CROSSES_MIDNIGHT


def test_midnight_start_same_day_is_fine():
    start, end = validate_booking_window(
        "2026-09-09T00:00:00+08:00", "2026-09-09T00:45:00+08:00"
    )
    assert start < end


def test_half_open_intervals_touch_but_do_not_overlap():
    a = Interval(parse_timestamp("2026-09-09T09:00:00+08:00"),
                 parse_timestamp("2026-09-09T10:00:00+08:00"))
    b = Interval(parse_timestamp("2026-09-09T10:00:00+08:00"),
                 parse_timestamp("2026-09-09T11:00:00+08:00"))
    assert not a.overlaps(b)
