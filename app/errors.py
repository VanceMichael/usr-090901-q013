"""稳定的结构化错误类型与错误码。

任何错误都必须可序列化为固定形状：
{"error": {"code": ..., "message": ..., "details": {...}}}
"""

from __future__ import annotations

from typing import Any


# 请求级错误码（整个载荷无法处理）
INVALID_BODY = "INVALID_BODY"
SCHEDULE_NOT_FOUND = "SCHEDULE_NOT_FOUND"
RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
NOT_FOUND = "NOT_FOUND"

# 单条预约级错误码（批量中互不影响）
SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
INVALID_TIMEZONE = "INVALID_TIMEZONE"
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
INVALID_INTERVAL = "INVALID_INTERVAL"
CROSSES_MIDNIGHT = "CROSSES_MIDNIGHT"
UNKNOWN_DEVICE = "UNKNOWN_DEVICE"
UNKNOWN_ZONE = "UNKNOWN_ZONE"
UNKNOWN_OPERATOR = "UNKNOWN_OPERATOR"
DEVICE_ZONE_INCOMPATIBLE = "DEVICE_ZONE_INCOMPATIBLE"
OPERATOR_NOT_AUTHORIZED = "OPERATOR_NOT_AUTHORIZED"
MAINTENANCE_WINDOW = "MAINTENANCE_WINDOW"
DEVICE_OVERLAP = "DEVICE_OVERLAP"
ZONE_CAPACITY = "ZONE_CAPACITY"
OPERATOR_OVERLAP = "OPERATOR_OVERLAP"
TURNAROUND_REQUIRED = "TURNAROUND_REQUIRED"

# 决策
DECISION_ALLOCATED = "allocated"
DECISION_DEFERRED = "deferred"
DECISION_CONFLICT = "conflict"
DECISION_ERROR = "error"


class StructuredError(Exception):
    """带稳定 code/details 的错误。"""

    status_code: int = 400

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None,
                 status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class BadRequest(StructuredError):
    status_code = 400


class NotFound(StructuredError):
    status_code = 404
