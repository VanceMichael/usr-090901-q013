"""HTTP API for the scheduling service (Python 3.12 standard library only).

Endpoints
---------
``GET  /healthz``                                          liveness probe
``GET  /v1/rules``                                         public rule view
``POST /v1/schedules``                                     allocate one batch
``GET  /v1/schedules/<schedule_id>/timeline``              stored timelines
``GET  /v1/resources/<type>/<id>/timeline?schedule_id=..`` one resource
``POST /v1/requests/explain``                              single-draft check

Allocation state is kept in process memory only: there is no database and
no connection to robots or campus systems. State resets when the process
restarts; the service is therefore safe to run as an ephemeral container.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import errors, timeutil
from .engine import (
    RESOURCE_DEVICE,
    RESOURCE_OPERATOR,
    RESOURCE_ZONE,
    ScheduleEngine,
    _CODE_PRIORITY,
    allocation_key,
    run_schedule,
    timeline_snapshot,
)
from .rules import Rules, load_default_rules
from .validator import ValidDraft, validate_batch, validate_draft

_MAX_BODY_BYTES = 1_048_576
_RESOURCE_TYPES = (RESOURCE_DEVICE, RESOURCE_ZONE, RESOURCE_OPERATOR)


class ScheduleStore:
    """Thread-safe in-process archive of the most recent batch per schedule."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._schedules: dict[str, dict] = {}

    def save(self, schedule_id: str, record: dict, engine: ScheduleEngine) -> None:
        with self._lock:
            self._schedules[schedule_id] = {
                "record": record,
                "engine": engine,
                "stored_at": timeutil.to_iso(datetime.now(tz=timeutil.LAB_TZ)),
            }

    def get(self, schedule_id: str) -> dict | None:
        with self._lock:
            return self._schedules.get(schedule_id)


def _error_body(code: str, message: str, **extra) -> dict:
    body = {"error": {"code": code, "message": message}}
    if extra:
        body["error"]["details"] = extra
    return body


class SchedulingHandler(BaseHTTPRequestHandler):
    rules: Rules = None  # type: ignore[assignment]  (injected by make_server)
    store: ScheduleStore = None  # type: ignore[assignment]

    server_version = "EmbodiedLabScheduler/1.0"

    # -- helpers ----------------------------------------------------------
    def _send_json(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, code: str, message: str, **details) -> None:
        self._send_json(status, _error_body(code, message, **details))

    def _read_json(self) -> tuple[object, dict | None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, _error_body(errors.BAD_JSON, "invalid Content-Length header")
        if length <= 0:
            return None, _error_body(errors.BAD_JSON, "request body is required")
        if length > _MAX_BODY_BYTES:
            return None, _error_body(
                errors.BAD_JSON, f"request body exceeds {_MAX_BODY_BYTES} bytes"
            )
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, _error_body(errors.BAD_JSON, f"body is not valid JSON: {exc}")

    def log_message(self, fmt: str, *args) -> None:  # quiet, structured stderr
        import sys

        sys.stderr.write(
            json.dumps(
                {"event": "http", "path": self.path, "message": fmt % args},
                ensure_ascii=False,
            )
            + "\n"
        )

    # -- routing ----------------------------------------------------------
    def _method_not_allowed(self, allowed: str) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", allowed)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = json.dumps(
            _error_body(
                errors.METHOD_NOT_ALLOWED,
                f"method {self.command} not allowed here; use {allowed}",
            ),
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parts = urlsplit(self.path)
        segments = [s for s in parts.path.split("/") if s]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}

        if segments == ["v1", "schedules"] or segments == [
            "v1",
            "requests",
            "explain",
        ]:
            self._method_not_allowed("POST")
            return
        if segments == ["healthz"]:
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if segments == ["v1", "rules"]:
            self._send_json(HTTPStatus.OK, self.rules.public_view())
            return
        if len(segments) == 4 and segments[:2] == ["v1", "schedules"] and segments[3] == "timeline":
            self._handle_timeline(segments[2], query)
            return
        if (
            len(segments) == 5
            and segments[:2] == ["v1", "resources"]
            and segments[4] == "timeline"
        ):
            self._handle_resource_timeline(
                query.get("schedule_id", ""),
                segments[2],
                segments[3],
            )
            return
        self._send_error(
            HTTPStatus.NOT_FOUND, errors.ROUTE_NOT_FOUND, f"unknown path: {parts.path}"
        )

    def do_POST(self) -> None:  # noqa: N802
        segments = [s for s in urlsplit(self.path).path.split("/") if s]
        if segments == ["v1", "schedules"]:
            self._handle_schedule()
        elif segments == ["v1", "requests", "explain"]:
            self._handle_explain()
        elif segments in (
            ["healthz"],
            ["v1", "rules"],
        ) or (len(segments) >= 3 and segments[:2] == ["v1", "schedules"]) or (
            len(segments) == 5
            and segments[:2] == ["v1", "resources"]
            and segments[4] == "timeline"
        ):
            self._method_not_allowed("GET")
        else:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                errors.ROUTE_NOT_FOUND,
                f"unknown path: {self.path}",
            )

    # -- endpoint handlers ------------------------------------------------
    def _handle_schedule(self) -> None:
        payload, bad = self._read_json()
        if bad is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, bad)
            return
        validation, envelope_error = validate_batch(payload, self.rules)
        if envelope_error is not None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": envelope_error.as_json()},
            )
            return

        results, engine = run_schedule(
            self.rules, validation.valid, validation.draft_errors
        )
        timelines = timeline_snapshot(engine)
        summary = {
            "received": len(results),
            "allocated": sum(r["decision"] == "allocated" for r in results),
            "deferred": sum(r["decision"] == "deferred" for r in results),
            "conflict": sum(r["decision"] == "conflict" for r in results),
            "rejected": sum(r["decision"] == "rejected" for r in results),
        }
        record = {
            "schedule_id": validation.schedule_id,
            "timezone": self.rules.timezone,
            "allocation_order": list(self.rules.allocation_order),
            "summary": summary,
            "results": results,
            "timelines": timelines,
        }
        self.store.save(validation.schedule_id, record, engine)
        # Per-draft problems are 200: the batch itself was processed.
        self._send_json(HTTPStatus.OK, record)

    def _load_schedule_or_404(self, schedule_id: str) -> dict | None:
        entry = self.store.get(schedule_id)
        if entry is None:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                errors.ROUTE_NOT_FOUND,
                f"unknown schedule_id: {schedule_id}",
            )
            return None
        return entry

    def _handle_timeline(self, schedule_id: str, query: dict) -> None:
        entry = self._load_schedule_or_404(schedule_id)
        if entry is None:
            return
        timelines = entry["record"]["timelines"]
        resource_type = query.get("resource_type")
        resource_id = query.get("resource_id")
        if resource_type is not None:
            if resource_type not in _RESOURCE_TYPES:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    errors.BAD_VALUE_RANGE,
                    f"resource_type must be one of: {', '.join(_RESOURCE_TYPES)}",
                )
                return
            if not resource_id:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    errors.MISSING_FIELD,
                    "resource_id query parameter is required with resource_type",
                )
                return
            key = f"{resource_type}:{resource_id}"
            if key not in timelines:
                self._send_error(
                    HTTPStatus.NOT_FOUND,
                    errors.ROUTE_NOT_FOUND,
                    f"unknown resource: {key}",
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "schedule_id": schedule_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "entries": timelines[key],
                },
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {"schedule_id": schedule_id, "timelines": timelines},
        )

    def _handle_resource_timeline(
        self, schedule_id: str, resource_type: str, resource_id: str
    ) -> None:
        """Resource-centric timeline: /v1/resources/<type>/<id>/timeline."""
        if not schedule_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                errors.MISSING_FIELD,
                "schedule_id query parameter is required",
            )
            return
        if resource_type not in _RESOURCE_TYPES:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                errors.ROUTE_NOT_FOUND,
                f"unknown resource type: {resource_type}",
            )
            return
        if not resource_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                errors.MISSING_FIELD,
                "resource_id query parameter is required",
            )
            return
        known = (
            resource_type == RESOURCE_DEVICE and self.rules.device(resource_id)
        ) or (
            resource_type == RESOURCE_ZONE and self.rules.zone(resource_id)
        ) or (
            resource_type == RESOURCE_OPERATOR and self.rules.operator(resource_id)
        )
        if not known:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                errors.ROUTE_NOT_FOUND,
                f"unknown {resource_type}: {resource_id}",
            )
            return
        entry = self._load_schedule_or_404(schedule_id)
        if entry is None:
            return
        key = f"{resource_type}:{resource_id}"
        self._send_json(
            HTTPStatus.OK,
            {
                "schedule_id": schedule_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "entries": entry["record"]["timelines"].get(key, []),
            },
        )

    def _handle_explain(self) -> None:
        payload, bad = self._read_json()
        if bad is not None:
            self._send_json(HTTPStatus.BAD_REQUEST, bad)
            return
        if not isinstance(payload, dict):
            self._send_error(
                HTTPStatus.BAD_REQUEST, errors.NOT_OBJECT, "body must be an object"
            )
            return
        allowed = {"request", "context", "context_schedule_id"}
        unexpected = sorted(set(payload) - allowed)
        if unexpected:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                errors.UNEXPECTED_FIELD,
                f"unexpected field(s): {', '.join(unexpected)}",
            )
            return
        if "request" not in payload:
            self._send_error(
                HTTPStatus.BAD_REQUEST, errors.MISSING_FIELD, "request is required"
            )
            return
        if not isinstance(payload["request"], dict):
            self._send_error(
                HTTPStatus.BAD_REQUEST, errors.BAD_TYPE, "request must be an object"
            )
            return
        if "context" in payload and "context_schedule_id" in payload:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                errors.BAD_VALUE_RANGE,
                "provide either context or context_schedule_id, not both",
            )
            return

        target_outcome = validate_draft(
            payload["request"], 0, self.rules, seen_ids=set()
        )
        if not isinstance(target_outcome, ValidDraft):
            self._send_json(
                HTTPStatus.OK,
                {
                    "decision": "rejected",
                    "request_id": target_outcome.request_id,
                    "errors": [target_outcome.as_json()],
                },
            )
            return
        target = target_outcome

        # Build the engine state the explanation is evaluated against.
        if "context_schedule_id" in payload:
            schedule_id = payload["context_schedule_id"]
            if not isinstance(schedule_id, str):
                self._send_error(
                    HTTPStatus.BAD_REQUEST, errors.BAD_TYPE,
                    "context_schedule_id must be a string",
                )
                return
            entry = self.store.get(schedule_id)
            if entry is None:
                self._send_error(
                    HTTPStatus.NOT_FOUND,
                    errors.ROUTE_NOT_FOUND,
                    f"unknown context_schedule_id: {schedule_id}",
                )
                return
            engine = entry["engine"]
        else:
            context_raw = payload.get("context", [])
            if not isinstance(context_raw, list) or len(context_raw) > 100:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    errors.BAD_LENGTH,
                    "context must be an array of at most 100 requests",
                )
                return
            # Context is an optional reference set (may be empty), so it is
            # validated item-by-item rather than through the batch envelope.
            engine = ScheduleEngine(self.rules)
            seen: set[str] = set()
            context_drafts: list[ValidDraft] = []
            for ctx_index, ctx_raw in enumerate(context_raw):
                outcome = validate_draft(ctx_raw, ctx_index, self.rules,
                                         seen_ids=seen)
                if not isinstance(outcome, ValidDraft):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": {
                                "code": outcome.code,
                                "message": outcome.message,
                                "field": outcome.field,
                                "context_index": ctx_index,
                            }
                        },
                    )
                    return
                seen.add(outcome.request_id)
                context_drafts.append(outcome)
            for ctx_draft in sorted(context_drafts, key=allocation_key):
                engine.evaluate(ctx_draft)

        inspection = engine.inspect(target)
        grouped = inspection["blocks"]
        present = {code: grouped[code] for code in _CODE_PRIORITY if grouped[code]}
        if present:
            # A hard block (maintenance/overlap/capacity) always outranks a
            # pure turnaround gap; only a gap-only result is "deferred".
            hard_codes = [c for c in present if c != errors.TURNAROUND_REQUIRED]
            primary_code = hard_codes[0] if hard_codes else errors.TURNAROUND_REQUIRED
            decision = "conflict" if hard_codes else "deferred"
            blocks = present[primary_code]
            conflicting = sorted(
                {f"{b.resource_type}:{b.resource_id}" for b in blocks}
            )
            blocking_ids = sorted(
                {b.request_id for b in blocks if b.request_id != "MAINTENANCE"}
            )
            later = [
                b.blocked_until
                for b in blocks
                if b.blocked_until > target.starts_at
            ]
            next_available = min(later or [b.blocked_until for b in blocks])
            self._send_json(
                HTTPStatus.OK,
                {
                    "decision": decision,
                    "request_id": target.request_id,
                    "conflict_code": primary_code,
                    "conflicting_resources": conflicting,
                    "blocking_request_ids": blocking_ids,
                    "blocked_by": [b.as_json() for b in blocks],
                    "next_available_at": timeutil.to_iso(next_available),
                    "required_clear_until": timeutil.to_iso(inspection["tail_end"]),
                    "all_checks": [
                        {
                            "conflict_code": code,
                            "conflicting_resources": sorted(
                                {
                                    f"{b.resource_type}:{b.resource_id}"
                                    for b in present[code]
                                }
                            ),
                            "blocked_by": [b.as_json() for b in present[code]],
                        }
                        for code in _CODE_PRIORITY
                        if code in present
                    ],
                },
            )
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "decision": "allocated",
                "request_id": target.request_id,
                "conflict_code": None,
                "conflicting_resources": [],
                "blocking_request_ids": [],
                "blocked_by": [],
                "next_available_at": None,
                "required_clear_until": timeutil.to_iso(inspection["tail_end"]),
                "all_checks": [],
            },
        )


def make_server(host: str, port: int, rules: Rules | None = None) -> ThreadingHTTPServer:
    if rules is None:
        rules = load_default_rules()
    store = ScheduleStore()

    # Per-server subclass so multiple servers in one process never share
    # mutable class attributes (rules set / in-memory store).
    handler = type(
        "BoundSchedulingHandler",
        (SchedulingHandler,),
        {"rules": rules, "store": store},
    )

    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.rules = rules  # type: ignore[attr-defined]
    httpd.store = store  # type: ignore[attr-defined]
    return httpd
