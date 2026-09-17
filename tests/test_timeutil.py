"""时间戳与时区校验测试。"""

from __future__ import annotations

import pytest

from app.errors import StructuredError
from app.timeutil import parse_shanghai


def test_explicit_offset_eight_ok():
    dt = parse_shanghai("2026-09-09T09:00:00+08:00")
    assert dt.isoformat() == "2026-09-09T09:00:00+08:00"


def test_zulu_rejected_as_illegal_timezone():
    with pytest.raises(StructuredError) as exc:
        parse_shanghai("2026-09-09T01:00:00Z")
    assert exc.value.code == "INVALID_TIMEZONE"


def test_other_offset_rejected():
    with pytest.raises(StructuredError) as exc:
        parse_shanghai("2026-09-09T09:00:00+00:00")
    assert exc.value.code == "INVALID_TIMEZONE"
    with pytest.raises(StructuredError):
        parse_shanghai("2026-09-09T10:00:00+09:00")


def test_naive_timestamp_rejected():
    with pytest.raises(StructuredError) as exc:
        parse_shanghai("2026-09-09T09:00:00")
    assert exc.value.code == "INVALID_TIMESTAMP"


def test_garbage_rejected():
    for bad in (None, 123, "", "not-a-date", "2026-13-40T99:00:00+08:00"):
        with pytest.raises(StructuredError):
            parse_shanghai(bad)


def test_fractional_seconds_ok():
    dt = parse_shanghai("2026-09-09T09:00:00.500+08:00")
    assert dt.microsecond == 500000
