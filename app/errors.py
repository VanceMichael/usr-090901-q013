"""Stable structured error codes shared by validation and the engine.

The service never raises uncaught exceptions for bad client input: every
rejectable condition is mapped to one of these codes and returned as a
structured error object so that one bad draft never aborts a whole batch.
"""

# Well-formedness / JSON-level errors
BAD_JSON = "BAD_JSON"
NOT_OBJECT = "NOT_OBJECT"
MISSING_FIELD = "MISSING_FIELD"
UNEXPECTED_FIELD = "UNEXPECTED_FIELD"
BAD_TYPE = "BAD_TYPE"
BAD_PATTERN = "BAD_PATTERN"
BAD_VALUE_RANGE = "BAD_VALUE_RANGE"
BAD_LENGTH = "BAD_LENGTH"
DUPLICATE_REQUEST_ID = "DUPLICATE_REQUEST_ID"

# Time / timezone errors
BAD_TIMEZONE = "BAD_TIMEZONE"
BAD_TIMESTAMP = "BAD_TIMESTAMP"
TIMEZONE_MISMATCH = "TIMEZONE_MISMATCH"
END_NOT_AFTER_START = "END_NOT_AFTER_START"
CROSSES_MIDNIGHT = "CROSSES_MIDNIGHT"

# Reference / compatibility errors
UNKNOWN_DEVICE = "UNKNOWN_DEVICE"
UNKNOWN_ZONE = "UNKNOWN_ZONE"
UNKNOWN_OPERATOR = "UNKNOWN_OPERATOR"
DEVICE_ZONE_INCOMPATIBLE = "DEVICE_ZONE_INCOMPATIBLE"
OPERATOR_NOT_AUTHORIZED = "OPERATOR_NOT_AUTHORIZED"

# Scheduling conflicts
DEVICE_OVERLAP = "DEVICE_OVERLAP"
ZONE_CAPACITY = "ZONE_CAPACITY"
OPERATOR_OVERLAP = "OPERATOR_OVERLAP"
MAINTENANCE_WINDOW = "MAINTENANCE_WINDOW"
TURNAROUND_REQUIRED = "TURNAROUND_REQUIRED"

# HTTP-layer errors
ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
RULES_LOAD_ERROR = "RULES_LOAD_ERROR"

# Error codes that are per-request *validation* errors (draft is rejected,
# never scheduled). Everything else produced by the engine is a conflict.
VALIDATION_CODES = frozenset(
    {
        MISSING_FIELD,
        UNEXPECTED_FIELD,
        BAD_TYPE,
        BAD_PATTERN,
        BAD_VALUE_RANGE,
        BAD_LENGTH,
        DUPLICATE_REQUEST_ID,
        BAD_TIMEZONE,
        BAD_TIMESTAMP,
        TIMEZONE_MISMATCH,
        END_NOT_AFTER_START,
        CROSSES_MIDNIGHT,
        UNKNOWN_DEVICE,
        UNKNOWN_ZONE,
        UNKNOWN_OPERATOR,
        DEVICE_ZONE_INCOMPATIBLE,
        OPERATOR_NOT_AUTHORIZED,
    }
)
