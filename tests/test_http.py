"""HTTP 端到端测试：单预约/批量、错误隔离与保序、时间线、解释、规则脱敏。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app.service import build_server


@pytest.fixture
def server():
    srv: ThreadingHTTPServer = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    base = f"http://{host}:{port}"
    yield base
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(server, method, path, body=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(server + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def req(rid, **over):
    base = {
        "request_id": rid,
        "device_id": "ARM-07",
        "zone_id": "ZONE-C",
        "operator_id": "OP-101",
        "starts_at": "2026-09-09T09:00:00+08:00",
        "ends_at": "2026-09-09T10:00:00+08:00",
        "priority": 3,
    }
    base.update(over)
    return base


# ---------------- 基础接口 ----------------

def test_healthz(server):
    status, body = call(server, "GET", "/healthz")
    assert status == 200 and body["status"] == "ok"


def test_rules_public_view_has_no_internal_notes(server):
    status, body = call(server, "GET", "/rules")
    assert status == 200
    assert "internal_notes" not in json.dumps(body, ensure_ascii=False)
    assert body["turnaround_minutes"] == 15
    assert body["allocation_order"] == ["priority_desc", "starts_at_asc", "request_id_asc"]
    assert set(body["conflict_codes"]) >= {
        "DEVICE_OVERLAP", "ZONE_CAPACITY", "OPERATOR_OVERLAP",
        "MAINTENANCE_WINDOW", "TURNAROUND_REQUIRED",
    }


def test_404_and_405_structured(server):
    status, body = call(server, "GET", "/nope")
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"
    status, body = call(server, "PUT", "/rules")
    assert status == 405 and body["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_malformed_json_is_structured_error(server):
    req_obj = urllib.request.Request(
        server + "/schedules", data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req_obj, timeout=5)
        assert False
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode())
        assert exc.code == 400 and body["error"]["code"] == "INVALID_BODY"


def test_envelope_schema_error(server):
    status, body = call(server, "POST", "/schedules", {
        "schedule_id": "BAD ID!!",
        "timezone": "Asia/Shanghai",
        "requests": [],
    })
    assert status == 400 and body["error"]["code"] == "SCHEMA_VIOLATION"


def test_illegal_timezone_envelope(server):
    status, body = call(server, "POST", "/schedules", {
        "schedule_id": "tz-schedule-0001",
        "timezone": "UTC",
        "requests": [req("REQ-TZ1")],
    })
    assert status == 400
    assert any(e["path"] == "$.timezone" for e in body["error"]["details"]["schema_errors"])


# ---------------- 单预约/批处理 ----------------

def test_single_schedule_and_persistence(server):
    payload = {"schedule_id": "http-sched-001", "timezone": "Asia/Shanghai",
               "requests": [req("REQ-H01")]}
    status, body = call(server, "POST", "/schedules", payload)
    assert status == 200
    assert body["results"][0]["decision"] == "allocated"

    status, body = call(server, "GET", "/schedules/http-sched-001")
    assert status == 200 and body["results"][0]["request_id"] == "REQ-H01"

    status, body = call(server, "GET", "/schedules/missing-schedule")
    assert status == 404 and body["error"]["code"] == "SCHEDULE_NOT_FOUND"


def test_batch_preserves_input_order_and_isolates_errors(server):
    good1 = {"schedule_id": "batch-good-0001", "timezone": "Asia/Shanghai",
             "requests": [
                 req("REQ-BG1", starts_at="2026-09-09T08:00:00+08:00",
                     ends_at="2026-09-09T08:30:00+08:00"),
             ]}
    bad_envelope = {"schedule_id": "x", "timezone": "Asia/Shanghai", "requests": []}
    mixed = {"schedule_id": "batch-mixed-0001", "timezone": "Asia/Shanghai",
             "requests": [
                 req("REQ-BM1", starts_at="2026-09-09T08:00:00+08:00",
                     ends_at="2026-09-09T08:30:00+08:00"),
                 req("REQ-BM2", device_id="GHOST-9", priority=2),
                 req("REQ-BM3",
                     starts_at="2026-09-09T12:10:00+08:00",
                     ends_at="2026-09-09T12:50:00+08:00", priority=5),
                 req("REQ-BM4",
                     starts_at="2026-09-09T23:40:00+08:00",
                     ends_at="2026-09-10T00:20:00+08:00"),
                 req("REQ-BM5",
                     starts_at="2026-09-09T09:00:00Z",
                     ends_at="2026-09-09T10:00:00+08:00"),
                 "not-an-object",
             ]}
    good2 = {"schedule_id": "batch-good-0002", "timezone": "Asia/Shanghai",
             "requests": [
                 req("REQ-BG2", device_id="ROVER-02", zone_id="ZONE-D",
                     operator_id="OP-202",
                     starts_at="2026-09-09T15:00:00+08:00",
                     ends_at="2026-09-09T15:30:00+08:00"),
             ]}
    status, body = call(server, "POST", "/schedules/batch",
                        {"schedules": [good1, bad_envelope, mixed, good2]})
    assert status == 200
    entries = body["schedules"]
    assert [e["input_index"] for e in entries] == [0, 1, 2, 3]

    assert entries[0]["status"] == "processed"
    assert entries[0]["result"]["results"][0]["decision"] == "allocated"
    assert entries[1]["error"]["code"] == "SCHEMA_VIOLATION"

    mixed_results = entries[2]["result"]["results"]
    assert [r["request_id"] for r in mixed_results] == [
        "REQ-BM1", "REQ-BM2", "REQ-BM3", "REQ-BM4", "REQ-BM5", "UNKNOWN-5"
    ]
    by = {r["request_id"]: r for r in mixed_results}
    assert by["REQ-BM1"]["decision"] == "allocated"
    assert by["REQ-BM2"]["decision"] == "error"
    assert by["REQ-BM2"]["error"]["code"] == "UNKNOWN_DEVICE"
    assert by["REQ-BM3"]["decision"] == "conflict"
    assert by["REQ-BM3"]["next_available_at"] == "2026-09-09T13:00:00+08:00"
    assert by["REQ-BM4"]["error"]["code"] == "CROSSES_MIDNIGHT"
    assert by["REQ-BM5"]["error"]["code"] == "INVALID_TIMEZONE"
    assert mixed_results[5]["error"]["code"] == "SCHEMA_VIOLATION"

    # 其他合法预约不受影响
    assert entries[3]["result"]["results"][0]["decision"] == "allocated"
    # 混合批次中成功的预约已持久化、可单独查询
    status, body = call(server, "GET", "/schedules/batch-good-0002")
    assert status == 200


def test_batch_wrong_shape(server):
    status, body = call(server, "POST", "/schedules/batch", {"nope": []})
    assert status == 400 and body["error"]["code"] == "INVALID_BODY"


# ---------------- 时间线与解释 ----------------

def test_timeline_device_includes_maintenance_and_booking(server):
    payload = {"schedule_id": "tl-sched-0001", "timezone": "Asia/Shanghai",
               "requests": [req("REQ-TL1",
                                starts_at="2026-09-09T09:00:00+08:00",
                                ends_at="2026-09-09T09:40:00+08:00")]}
    call(server, "POST", "/schedules", payload)
    status, body = call(
        server, "GET",
        "/schedules/tl-sched-0001/timeline?resource_type=device&resource_id=ARM-07")
    assert status == 200
    kinds = [(item["kind"], item.get("request_id")) for item in body["timeline"]]
    assert ("allocated", "REQ-TL1") in kinds
    assert any(k == "maintenance" for k, _ in kinds)
    # 时间线按开始时间排序
    starts = [item["starts_at"] for item in body["timeline"]]
    assert starts == sorted(starts)


def test_timeline_unknown_resource_404(server):
    payload = {"schedule_id": "tl-sched-0002", "timezone": "Asia/Shanghai",
               "requests": [req("REQ-TL2")]}
    call(server, "POST", "/schedules", payload)
    status, body = call(
        server, "GET",
        "/schedules/tl-sched-0002/timeline?resource_type=device&resource_id=NOPE")
    assert status == 404 and body["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_request_explanation(server):
    payload = {"schedule_id": "ex-sched-0001", "timezone": "Asia/Shanghai",
               "requests": [
                   req("REQ-EX1", priority=5,
                       starts_at="2026-09-09T09:00:00+08:00",
                       ends_at="2026-09-09T10:30:00+08:00"),
                   req("REQ-EX2", operator_id="OP-202", priority=3,
                       starts_at="2026-09-09T09:30:00+08:00",
                       ends_at="2026-09-09T10:00:00+08:00"),
               ]}
    call(server, "POST", "/schedules", payload)
    status, body = call(server, "GET", "/schedules/ex-sched-0001/requests/REQ-EX2")
    assert status == 200
    assert body["decision"] == "deferred"
    assert "REQ-EX1" in body["blocking_request_ids"]
    assert isinstance(body["explanation"], list) and body["explanation"]
    assert any("REQ-EX1" in line for line in body["explanation"])

    status, body = call(server, "GET", "/schedules/ex-sched-0001/requests/REQ-GHOST")
    assert status == 404 and body["error"]["code"] == "REQUEST_NOT_FOUND"


def test_no_internal_notes_leaked_anywhere(server):
    payload = {"schedule_id": "sec-sched-001", "timezone": "Asia/Shanghai",
               "requests": [
                   req("REQ-SE1", device_id="GHOST-X"),
                   req("REQ-SE2",
                       starts_at="2026-09-09T12:00:00+08:00",
                       ends_at="2026-09-09T12:30:00+08:00"),
               ]}
    _, body = call(server, "POST", "/schedules", payload)
    raw = json.dumps(body, ensure_ascii=False)
    assert "internal_notes" not in raw and "Allocation test fixture" not in raw
