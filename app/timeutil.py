"""Timestamp parsing and Asia/Shanghai lab-calendar rules.

All public timestamps must be explicit RFC 3339 date-time values pinned to
the lab timezone (``Asia/Shanghai``). Naive strings, offsets for other
zones, and drafts crossing the Shanghai midnight boundary are rejected
upstream of the allocation engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LAB_TIMEZONE = "Asia/Shanghai"
LAB_TZ = ZoneInfo(LAB_TIMEZONE)


class TimeParseError(ValueError):
    """Carries the reason a timestamp string could not be accepted."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Interval:
    """Half-open busy interval ``[starts_at, ends_at)`` in lab time."""

    starts_at: datetime
    ends_at: datetime

    def overlaps(self, other: Interval) -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


def parse_timestamp(value: object, *, field: str = "timestamp") -> datetime:
    """Parse an RFC 3339 timestamp; return aware datetime or TimeParseError.

    The caller decides how to surface the returned error; it is returned
    rather than raised so batch validation can collect it structurally.
    """
    if not isinstance(value, str):
        return TimeParseError("BAD_TYPE", f"{field} must be an RFC 3339 string")
    text = value.strip()
    if not text:
        return TimeParseError("BAD_TIMESTAMP", f"{field} must not be empty")
    # datetime.fromisoformat accepts a trailing 'Z' on 3.11+. Normalise it so
    # the offset comparison below gives a stable mismatch error.
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return TimeParseError(
            "BAD_TIMESTAMP",
            f"{field} is not a valid RFC 3339 date-time: {value!r}",
        )
    if parsed.tzinfo is None:
        return TimeParseError(
            "BAD_TIMESTAMP",
            f"{field} must include an explicit UTC offset (use +08:00)",
        )
    # Reject offsets that do not belong to Asia/Shanghai at that instant
    # (e.g. 'Z' / +00:00, +09:00). Compare via UTC normalisation to stay
    # correct around any offset rules.
    local = parsed.astimezone(LAB_TZ)
    if local.utcoffset() != parsed.utcoffset():
        return TimeParseError(
            "TIMEZONE_MISMATCH",
            f"{field} offset must be expressed in {LAB_TIMEZONE} (+08:00), got {value!r}",
        )
    return local


def validate_booking_window(
    starts_at: object, ends_at: object
) -> tuple[datetime, datetime] | tuple[TimeParseError, TimeParseError | None]:
    """Validate the (start, end) pair beyond single-field parsing.

    Returns ``(start, end)`` on success or ``(error, second_error | None)``.
    """
    start = parse_timestamp(starts_at, field="starts_at")
    end = parse_timestamp(ends_at, field="ends_at")
    start_err = start if isinstance(start, TimeParseError) else None
    end_err = end if isinstance(end, TimeParseError) else None
    if start_err or end_err:
        return (start_err or end_err), (end_err if start_err is None else None)
    if end <= start:
        return TimeParseError(
            "END_NOT_AFTER_START",
            "ends_at must be strictly later than starts_at",
        ), None
    if start.date() != end.date():
        return TimeParseError(
            "CROSSES_MIDNIGHT",
            "bookings must stay within one Asia/Shanghai calendar day "
            "(no cross-midnight intervals)",
        ), None
    return start, end


def to_iso(dt: datetime) -> str:
    """Serialise an aware datetime as a Shanghai offset string.

    Sub-second precision is preserved when present; zero microseconds are
    emitted as whole seconds (``datetime.isoformat`` ``auto`` timespec).
    """
    local = dt.astimezone(LAB_TZ)
    return local.isoformat()


def add_minutes(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=minutes)
