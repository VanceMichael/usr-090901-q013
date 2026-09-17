"""时间工具：只接受 Asia/Shanghai（GMT+8，无夏令时）。

时间戳必须是 RFC 3339 date-time 且显式携带偏移或 Z；
不允许挂名时区与实际偏移不一致（例如 timezone=Asia/Shanghai 却写 +00:00）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .errors import INVALID_TIMESTAMP, INVALID_TIMEZONE, StructuredError

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
EXPECTED_OFFSETS = (timedelta(hours=8),)

# RFC 3339 / ISO 8601 子集：必须显式带时区偏移或 Z
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def parse_shanghai(value: object, *, field: str = "timestamp") -> datetime:
    """把时间戳解析为 tz-aware datetime（固定 +08:00）。"""
    if not isinstance(value, str) or not _RFC3339.match(value):
        raise StructuredError(
            INVALID_TIMESTAMP,
            f"字段 {field} 必须是带时区偏移的 RFC3339 时间戳",
            {"field": field, "value": value},
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StructuredError(
            INVALID_TIMESTAMP,
            f"字段 {field} 无法解析",
            {"field": field, "value": value},
        ) from None
    if parsed.tzinfo is None:
        raise StructuredError(
            INVALID_TIMESTAMP,
            f"字段 {field} 必须显式携带时区偏移",
            {"field": field, "value": value},
        )
    offset = parsed.utcoffset()
    if offset not in EXPECTED_OFFSETS:
        # 非法时区：声明 Asia/Shanghai 却使用其他偏移
        raise StructuredError(
            INVALID_TIMEZONE,
            "时间戳必须使用 Asia/Shanghai(+08:00) 偏移",
            {"field": field, "value": value, "expected_offset": "+08:00"},
        )
    # 统一折叠为固定 +8 时区对象
    return parsed.replace(tzinfo=SHANGHAI)


def ensure_declared_timezone(value: object) -> None:
    if value != "Asia/Shanghai":
        raise StructuredError(
            INVALID_TIMEZONE,
            "仅支持 Asia/Shanghai 时区",
            {"field": "timezone", "value": value, "allowed": ["Asia/Shanghai"]},
        )


def iso(dt: datetime) -> str:
    """序列化为带 +08:00 的字符串。"""
    return dt.astimezone(SHANGHAI).isoformat()


def same_calendar_day(a: datetime, b: datetime) -> bool:
    """按上海本地日期判断是否同一自然日（跨午夜检查）。"""
    return a.astimezone(SHANGHAI).date() == b.astimezone(SHANGHAI).date()
