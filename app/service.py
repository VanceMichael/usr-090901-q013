"""HTTP 服务（stdlib http.server，无外部网络依赖）。

接口：
  GET  /healthz
  GET  /rules
  POST /schedules                      单个排程（契约：contracts/request.schema.json）
  POST /schedules/batch                批量排程 {"schedules": [...]}，结果保序、错误隔离
  GET  /schedules/{id}                 排程结果
  GET  /schedules/{id}/timeline?resource_type=device|zone|operator&resource_id=...
  GET  /schedules/{id}/requests/{rid}  单预约冲突解释
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .engine import Booking, Schedule
from .errors import (
    INVALID_BODY,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    REQUEST_NOT_FOUND,
    SCHEDULE_NOT_FOUND,
    SCHEMA_VIOLATION,
    DECISION_ERROR,
    StructuredError,
)
from .rules import RulesLoader, strip_secrets
from .timeutil import ensure_declared_timezone, iso

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "contracts" / "request.schema.json"

DUPLICATE_REQUEST_ID = "DUPLICATE_REQUEST_ID"


class ScheduleRegistry:
    """内存态排程结果仓库（进程内，服务重启即清空）。"""

    def __init__(self) -> None:
        self._schedules: dict[str, Schedule] = {}
        self._lock = threading.Lock()

    def put(self, schedule: Schedule) -> None:
        with self._lock:
            self._schedules[schedule.schedule_id] = schedule

    def get(self, schedule_id: str) -> Schedule:
        with self._lock:
            schedule = self._schedules.get(schedule_id)
        if schedule is None:
            raise StructuredError(
                SCHEDULE_NOT_FOUND, f"排程不存在：{schedule_id}",
                {"schedule_id": schedule_id}, status_code=404,
            )
        return schedule


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def schema_request_errors(req: Any, index: int) -> list[dict[str, Any]]:
    """对单条预约做 JSON Schema 校验，返回错误列表。"""
    if jsonschema is None:  # pragma: no cover
        return []
    contract = load_contract()
    validator = jsonschema.Draft202012Validator(
        contract["properties"]["requests"]["items"],
        format_checker=jsonschema.FormatChecker(),
    )
    errors = []
    for e in validator.iter_errors(req):
        errors.append({
            "path": "$.requests[%d].%s" % (index, ".".join(str(p) for p in e.absolute_path)),
            "message": e.message,
        })
    return errors


def schema_envelope_errors(payload: Any) -> list[dict[str, Any]]:
    if jsonschema is None:  # pragma: no cover
        return []
    # 不校验 requests 内部（逐条隔离处理），只校验信封与 requests 容器
    contract = load_contract()
    validator = jsonschema.Draft202012Validator(contract)
    errors = []
    for e in validator.iter_errors(payload):
        # 跳过 requests.items 内部错误，逐条单独报
        if list(e.absolute_path)[:1] == ["requests"] and len(list(e.absolute_path)) > 1:
            continue
        errors.append({
            "path": "$." + ".".join(str(p) for p in e.absolute_path),
            "message": e.message,
        })
    return errors


def _request_error_result(rid: str, index: int, code: str, message: str,
                          details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = details or {}
    payload.setdefault("request_id", rid)
    return {
        "request_id": rid,
        "decision": DECISION_ERROR,
        "conflicts": [],
        "blocking_request_ids": [],
        "next_available_at": None,
        "allocated_resources": [],
        "error": {"code": code, "message": message, "details": payload},
        "_input_index": index,
    }


def process_schedule(payload: dict[str, Any], rules_loader: RulesLoader) -> Schedule:
    """执行一次排程，返回 Schedule（含按输入顺序的结果）。"""
    rules = rules_loader.get()
    schedule = Schedule(payload["schedule_id"], rules)

    seen: set[str] = set()
    valid: list[Booking] = []
    ordered_results: list[tuple[int, str, dict[str, Any] | None]] = []

    for index, req in enumerate(payload["requests"]):
        rid = req.get("request_id", f"UNKNOWN-{index}") if isinstance(req, dict) else f"UNKNOWN-{index}"

        if not isinstance(req, dict):
            ordered_results.append((index, rid, _request_error_result(
                rid, index, SCHEMA_VIOLATION, "每条预约必须是 JSON 对象",
                {"value": req})))
            continue

        schema_errors = schema_request_errors(req, index)
        if schema_errors:
            ordered_results.append((index, rid, _request_error_result(
                rid, index, SCHEMA_VIOLATION, "预约不符合请求契约",
                {"schema_errors": schema_errors})))
            continue

        if rid in seen:
            ordered_results.append((index, rid, _request_error_result(
                rid, index, DUPLICATE_REQUEST_ID, "同一排程内 request_id 重复",
                {"request_id": rid})))
            continue
        seen.add(rid)

        req_with_index = dict(req)
        req_with_index["_input_index"] = index
        booking, error = schedule.validate_request(req_with_index)
        if error is not None:
            ordered_results.append((index, rid, _request_error_result(
                rid, index, error["code"], error["message"], error["details"])))
        else:
            assert booking is not None
            valid.append(booking)

    schedule.allocate(valid)

    # 错误结果按输入位置保存（不参与分配，也不会与合法 rid 互相覆盖）
    schedule.error_results = {index: _ErrorResult(result)
                              for index, _rid, result in ordered_results}
    schedule.input_order = [
        (i, r.get("request_id", f"UNKNOWN-{i}") if isinstance(r, dict) else f"UNKNOWN-{i}")
        for i, r in enumerate(payload["requests"])
    ]
    return schedule


class _ErrorResult:
    """引擎外部构造的错误结果，to_dict 返回去掉内部键的载荷。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.input_index = payload["_input_index"]

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self._payload.items() if not k.startswith("_")}


def schedule_response(schedule: Schedule) -> dict[str, Any]:
    results = []
    for index, rid in schedule.input_order:
        result = schedule.results.get(rid)
        if result is None:
            result = schedule.error_results.get(index)
        if result is not None:
            results.append(strip_secrets(result.to_dict()))
    return {
        "schedule_id": schedule.schedule_id,
        "rules_version": schedule.rules.version,
        "timezone": schedule.rules.timezone,
        "results": results,
    }


def explanation(schedule: Schedule, request_id: str) -> dict[str, Any]:
    result = schedule.results.get(request_id)
    if result is None:
        for error_result in schedule.error_results.values():
            if error_result.to_dict().get("request_id") == request_id:
                result = error_result
                break
    if result is None:
        raise StructuredError(
            REQUEST_NOT_FOUND, f"预约不存在：{request_id}",
            {"schedule_id": schedule.schedule_id, "request_id": request_id},
            status_code=404,
        )
    body = result.to_dict()
    lines = [f"request_id={request_id} 的判定：{body['decision']}。"]
    if body["decision"] == "allocated":
        lines.append("设备、区域与指导员在请求时段内均可用，已按分配顺序占用资源。")
    if body.get("error"):
        lines.append(f"错误 {body['error']['code']}：{body['error']['message']}。")
    for c in body.get("conflicts", []):
        blockers = ", ".join(c.get("blocking_request_ids", [])) or "无"
        lines.append(
            f"资源 {c['resource_type']}:{c['resource_id']} 命中 {c['code']}；"
            f"阻塞预约={blockers}；该资源下一可用={c.get('next_available_at')}。"
        )
    if body.get("next_available_at"):
        lines.append(f"综合三类资源后的下一可用时段：{body['next_available_at']}。")
    body["explanation"] = lines
    return strip_secrets(body)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "EmbodiedLabScheduler/1.0"
    registry: ScheduleRegistry
    rules_loader: RulesLoader

    # ---- 框架 ----
    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: StructuredError) -> None:
        self._send_json(exc.status_code, {"error": exc.to_dict()})

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise StructuredError(INVALID_BODY, "请求体为空", status_code=400)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StructuredError(INVALID_BODY, f"请求体不是合法 JSON：{exc}",
                                  status_code=400) from None

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        try:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            query = parse_qs(urlsplit(self.path).query)
            if parts == ["healthz"]:
                self._send_json(200, {"status": "ok", "time": iso(datetime.now().astimezone())})
            elif parts == ["rules"]:
                self._send_json(200, self.rules_loader.get().public_view())
            elif len(parts) == 2 and parts[0] == "schedules":
                schedule = self.registry.get(parts[1])
                self._send_json(200, schedule_response(schedule))
            elif len(parts) == 3 and parts[0] == "schedules" and parts[2] == "timeline":
                schedule = self.registry.get(parts[1])
                rtype = (query.get("resource_type") or [""])[0]
                rid = (query.get("resource_id") or [""])[0]
                self._send_json(200, {
                    "schedule_id": schedule.schedule_id,
                    "resource_type": rtype,
                    "resource_id": rid,
                    "timeline": schedule.timeline(rtype, rid),
                })
            elif len(parts) == 4 and parts[0] == "schedules" and parts[2] == "requests":
                schedule = self.registry.get(parts[1])
                self._send_json(200, {
                    "schedule_id": schedule.schedule_id,
                    **explanation(schedule, parts[3]),
                })
            else:
                raise StructuredError(NOT_FOUND, "路径不存在", {"path": self.path},
                                      status_code=404)
        except StructuredError as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            if parts == ["schedules"]:
                payload = self._read_json()
                envelope_errors = self._validate_envelope(payload)
                if envelope_errors:
                    raise StructuredError(
                        SCHEMA_VIOLATION, "请求信封不符合契约",
                        {"schema_errors": envelope_errors}, status_code=400,
                    )
                schedule = process_schedule(payload, self.rules_loader)
                self.registry.put(schedule)
                self._send_json(200, schedule_response(schedule))
            elif parts == ["schedules", "batch"]:
                body = self._read_json()
                if not isinstance(body, dict) or not isinstance(body.get("schedules"), list):
                    raise StructuredError(
                        INVALID_BODY, '批量请求体必须形如 {"schedules": [...]}',
                        status_code=400,
                    )
                if not 1 <= len(body["schedules"]) <= 50:
                    raise StructuredError(
                        INVALID_BODY, "批量排程数量必须在 1..50 之间",
                        {"count": len(body["schedules"])}, status_code=400,
                    )
                entries = []
                for index, item in enumerate(body["schedules"]):
                    entry: dict[str, Any] = {"input_index": index}
                    envelope_errors = self._validate_envelope(item)
                    if envelope_errors:
                        entry["error"] = {
                            "code": SCHEMA_VIOLATION,
                            "message": "请求信封不符合契约",
                            "details": {"schema_errors": envelope_errors, "input_index": index},
                        }
                    else:
                        try:
                            schedule = process_schedule(item, self.rules_loader)
                            self.registry.put(schedule)
                            entry["schedule_id"] = schedule.schedule_id
                            entry["status"] = "processed"
                            entry["result"] = schedule_response(schedule)
                        except StructuredError as exc:  # pragma: no cover - 兜底
                            entry["error"] = exc.to_dict()
                    entries.append(entry)
                self._send_json(200, {"schedules": entries})
            else:
                raise StructuredError(NOT_FOUND, "路径不存在", {"path": self.path},
                                      status_code=404)
        except StructuredError as exc:
            self._error(exc)

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._error(StructuredError(METHOD_NOT_ALLOWED, "不支持的 HTTP 方法",
                                    {"method": self.command}, status_code=405))

    def _validate_envelope(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return [{"path": "$", "message": "请求体必须是 JSON 对象"}]
        errors = schema_envelope_errors(payload)
        # timezone 常量由 schema const 覆盖；这里保留语义层显式检查的错误信息风格
        if isinstance(payload.get("timezone"), str) and payload["timezone"] != "Asia/Shanghai":
            try:
                ensure_declared_timezone(payload["timezone"])
            except StructuredError as exc:
                errors.append({"path": "$.timezone", "message": exc.message})
        return errors


def build_server(host: str = "0.0.0.0", port: int = 8080,
                 rules_path: str | Path | None = None) -> ThreadingHTTPServer:
    loader = RulesLoader(rules_path)
    registry = ScheduleRegistry()

    class _Handler(AppHandler):
        pass

    _Handler.registry = registry
    _Handler.rules_loader = loader
    server = ThreadingHTTPServer((host, port), _Handler)
    return server
