"""Deterministic per-draft validation implementing contracts/request.schema.json.

A manual validator is used (instead of a hard third-party dependency) so the
runtime image needs nothing but the Python 3.12 standard library. Its rules
mirror the JSON Schema contract exactly; ``tests/test_contract.py`` addition‑
ally cross-checks both with ``jsonschema`` when it is installed.

Validation never raises: malformed drafts are returned as structured error
lists and simply skipped by the allocator, so one bad draft cannot affect the
others in a batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import errors
from .rules import Rules
from .timeutil import TimeParseError, parse_timestamp

_REQUEST_FIELDS = (
    "request_id",
    "device_id",
    "zone_id",
    "operator_id",
    "starts_at",
    "ends_at",
    "priority",
    "charging_minutes_after",
)
_REQUIRED_REQUEST_FIELDS = _REQUEST_FIELDS[:-1]


@dataclass
class ValidDraft:
    index: int
    request_id: str
    device_id: str
    zone_id: str
    operator_id: str
    starts_at: datetime
    ends_at: datetime
    priority: int
    charging_minutes_after: int


@dataclass
class DraftError:
    index: int
    request_id: str | None
    code: str
    message: str
    field: str | None = None
    value: Any = None

    def as_json(self) -> dict:
        body = {
            "index": self.index,
            "request_id": self.request_id,
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            body["field"] = self.field
        if self.value is not None or self.field is not None:
            body["value"] = self.value
        return body


@dataclass
class ScheduleValidation:
    schedule_id: str | None = None
    valid: list[ValidDraft] = field(default_factory=list)
    draft_errors: list[DraftError] = field(default_factory=list)
    duplicate_ids: set[str] = field(default_factory=set)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int but is never an acceptable integer field.
    return isinstance(value, int) and not isinstance(value, bool)


def _draft_error(
    index: int, request_id: str | None, code: str, message: str, fld: str, value: Any
) -> DraftError:
    return DraftError(index, request_id, code, message, fld, value)


def validate_draft(
    raw: Any, index: int, rules: Rules, *, seen_ids: set[str]
) -> ValidDraft | DraftError:
    """Validate one request object.

    Returns a :class:`ValidDraft`, or a single :class:`DraftError`. Field
    checks run in a fixed order and stop at the first failed stage so error
    codes stay deterministic; reference checks are only reached when the
    draft is otherwise well-formed.
    """
    if not isinstance(raw, dict):
        return DraftError(
            index,
            None,
            errors.NOT_OBJECT,
            "each request must be a JSON object",
            field=None,
            value=None,
        )

    rid_value = raw.get("request_id")
    request_id = rid_value if isinstance(rid_value, str) else None

    # Unknown fields first (contract: additionalProperties=false).
    unexpected = sorted(k for k in raw if k not in _REQUEST_FIELDS)
    if unexpected:
        return _draft_error(
            index,
            request_id,
            errors.UNEXPECTED_FIELD,
            f"unexpected field(s): {', '.join(unexpected)}",
            unexpected[0],
            raw[unexpected[0]],
        )

    # Required-field presence (fixed field order, deterministic choice).
    for fld in _REQUIRED_REQUEST_FIELDS:
        if fld not in raw:
            return _draft_error(
                index, request_id, errors.MISSING_FIELD, f"{fld} is required", fld, None
            )

    # String fields.
    for fld in ("device_id", "zone_id", "operator_id"):
        if not isinstance(raw[fld], str) or not raw[fld].strip():
            return _draft_error(
                index,
                request_id,
                errors.BAD_TYPE,
                f"{fld} must be a non-empty string",
                fld,
                raw[fld],
            )

    if not isinstance(raw["request_id"], str):
        return _draft_error(
            index, None, errors.BAD_TYPE, "request_id must be a string", "request_id",
            raw["request_id"],
        )
    request_id = raw["request_id"]
    # ^REQ-[A-Z0-9-]{3,24}$
    suffix = request_id[4:] if request_id.startswith("REQ-") else None
    if (
        suffix is None
        or not (3 <= len(suffix) <= 24)
        or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for ch in suffix)
    ):
        return _draft_error(
            index,
            request_id,
            errors.BAD_PATTERN,
            "request_id must match ^REQ-[A-Z0-9-]{3,24}$",
            "request_id",
            request_id,
        )

    if request_id in seen_ids:
        return _draft_error(
            index,
            request_id,
            errors.DUPLICATE_REQUEST_ID,
            f"request_id {request_id} appears more than once in this batch",
            "request_id",
            request_id,
        )

    if not _is_int(raw["priority"]) or not 1 <= raw["priority"] <= 5:
        return _draft_error(
            index,
            request_id,
            errors.BAD_VALUE_RANGE,
            "priority must be an integer between 1 and 5",
            "priority",
            raw["priority"],
        )

    charging = raw.get("charging_minutes_after", 0)
    if not _is_int(charging) or not 0 <= charging <= 240:
        return _draft_error(
            index,
            request_id,
            errors.BAD_VALUE_RANGE,
            "charging_minutes_after must be an integer between 0 and 240",
            "charging_minutes_after",
            charging,
        )

    # Timestamps: explicit Shanghai offset, ordering, same calendar day.
    starts_at = parse_timestamp(raw["starts_at"], field="starts_at")
    if isinstance(starts_at, TimeParseError):
        return _draft_error(
            index, request_id, starts_at.code, starts_at.message,
            "starts_at", raw["starts_at"],
        )
    ends_at = parse_timestamp(raw["ends_at"], field="ends_at")
    if isinstance(ends_at, TimeParseError):
        return _draft_error(
            index, request_id, ends_at.code, ends_at.message,
            "ends_at", raw["ends_at"],
        )
    if ends_at <= starts_at:
        return _draft_error(
            index,
            request_id,
            errors.END_NOT_AFTER_START,
            "ends_at must be strictly later than starts_at",
            "ends_at",
            raw["ends_at"],
        )
    if starts_at.date() != ends_at.date():
        return _draft_error(
            index,
            request_id,
            errors.CROSSES_MIDNIGHT,
            "bookings must stay within one Asia/Shanghai calendar day "
            "(no cross-midnight intervals)",
            "ends_at",
            raw["ends_at"],
        )

    # Reference and authorisation checks (only with well-formed references).
    device = rules.device(raw["device_id"])
    if device is None:
        return _draft_error(
            index,
            request_id,
            errors.UNKNOWN_DEVICE,
            f"unknown device_id: {raw['device_id']}",
            "device_id",
            raw["device_id"],
        )
    zone = rules.zone(raw["zone_id"])
    if zone is None:
        return _draft_error(
            index,
            request_id,
            errors.UNKNOWN_ZONE,
            f"unknown zone_id: {raw['zone_id']}",
            "zone_id",
            raw["zone_id"],
        )
    operator = rules.operator(raw["operator_id"])
    if operator is None:
        return _draft_error(
            index,
            request_id,
            errors.UNKNOWN_OPERATOR,
            f"unknown operator_id: {raw['operator_id']}",
            "operator_id",
            raw["operator_id"],
        )

    if raw["zone_id"] not in device.allowed_zones:
        return _draft_error(
            index,
            request_id,
            errors.DEVICE_ZONE_INCOMPATIBLE,
            f"device {device.device_id} is not allowed in zone {zone.zone_id} "
            f"(allowed: {', '.join(device.allowed_zones)})",
            "zone_id",
            raw["zone_id"],
        )

    if device.device_id not in operator.devices:
        return _draft_error(
            index,
            request_id,
            errors.OPERATOR_NOT_AUTHORIZED,
            f"operator {operator.operator_id} is not authorised for device "
            f"{device.device_id}",
            "operator_id",
            raw["operator_id"],
        )

    return ValidDraft(
        index=index,
        request_id=request_id,
        device_id=device.device_id,
        zone_id=zone.zone_id,
        operator_id=operator.operator_id,
        starts_at=starts_at,
        ends_at=ends_at,
        priority=raw["priority"],
        charging_minutes_after=charging,
    )


def validate_batch(
    payload: Any, rules: Rules
) -> tuple[ScheduleValidation | None, DraftError | None]:
    """Validate the schedule envelope and every draft.

    Returns ``(validation, None)`` on success or ``(None, envelope_error)``
    when the envelope itself is unusable (HTTP 400). Per-draft problems live
    inside the validation result and never abort the batch.
    """
    if not isinstance(payload, dict):
        return None, DraftError(
            -1, None, errors.NOT_OBJECT, "request body must be a JSON object", None, None
        )

    unexpected = sorted(
        k for k in payload if k not in ("schedule_id", "timezone", "requests")
    )
    if unexpected:
        return None, DraftError(
            -1,
            None,
            errors.UNEXPECTED_FIELD,
            f"unexpected field(s): {', '.join(unexpected)}",
            unexpected[0],
            payload[unexpected[0]],
        )

    if "schedule_id" not in payload:
        return None, DraftError(
            -1, None, errors.MISSING_FIELD, "schedule_id is required", "schedule_id", None
        )
    schedule_id = payload["schedule_id"]
    if not isinstance(schedule_id, str) or not (
        8 <= len(schedule_id) <= 48
        and all(ch in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in schedule_id)
    ):
        return None, DraftError(
            -1,
            None,
            errors.BAD_PATTERN,
            "schedule_id must match ^[a-z0-9-]{8,48}$",
            "schedule_id",
            schedule_id,
        )

    if "timezone" not in payload:
        return None, DraftError(
            -1, None, errors.MISSING_FIELD, "timezone is required", "timezone", None
        )
    if payload["timezone"] != rules.timezone:
        code = errors.BAD_TIMEZONE if isinstance(payload["timezone"], str) else errors.BAD_TYPE
        return None, DraftError(
            -1,
            None,
            code,
            f"timezone must be the const {rules.timezone!r}",
            "timezone",
            payload["timezone"],
        )

    if "requests" not in payload:
        return None, DraftError(
            -1, None, errors.MISSING_FIELD, "requests is required", "requests", None
        )
    raw_requests = payload["requests"]
    if not isinstance(raw_requests, list):
        return None, DraftError(
            -1, None, errors.BAD_TYPE, "requests must be an array", "requests",
            raw_requests,
        )
    if not 1 <= len(raw_requests) <= 100:
        return None, DraftError(
            -1,
            None,
            errors.BAD_LENGTH,
            "requests must contain between 1 and 100 items",
            "requests",
            len(raw_requests) if isinstance(raw_requests, list) else None,
        )

    result = ScheduleValidation(schedule_id=schedule_id)
    seen: set[str] = set()
    for index, raw in enumerate(raw_requests):
        outcome = validate_draft(raw, index, rules, seen_ids=seen)
        if isinstance(outcome, ValidDraft):
            seen.add(outcome.request_id)
            result.valid.append(outcome)
        else:
            result.draft_errors.append(outcome)
            if outcome.code == errors.DUPLICATE_REQUEST_ID:
                result.duplicate_ids.add(outcome.request_id)
    return result, None
