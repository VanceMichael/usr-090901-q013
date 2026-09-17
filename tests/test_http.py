"""End-to-end HTTP tests against the real stdlib server (ephemeral port)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app.rules import load_rules
from app.server import make_server

from .conftest import REAL_RULES, draft, envelope


@pytest.fixture(scope="module")
def server():
    httpd = make_server("127.0.0.1", 0, rules=load_rules(REAL_RULES))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def request(server, method, path, body=None, *, expect_status=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        server + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            status = resp.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        status = exc.code
    if expect_status is not None:
        assert status == expect_status, payload
    return status, payload


# --------------------------------------------------------------- plumbing
def test_healthz(server):
    status, payload = request(server, "GET", "/healthz")
    assert status == 200 and payload == {"status": "ok"}


def test_unknown_route_is_structured_404(server):
    status, payload = request(server, "GET", "/nope", expect_status=404)
    assert payload["error"]["code"] == "ROUTE_NOT_FOUND"


def test_wrong_method_is_structured_405(server):
    status, payload = request(server, "GET", "/v1/schedules", expect_status=405)
    assert payload["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_bad_json_is_structured_400(server):
    req = urllib.request.Request(
        server + "/v1/schedules",
        data=b"{not json",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    payload = json.loads(exc_info.value.read().decode())
    assert exc_info.value.code == 400
    assert payload["error"]["code"] == "BAD_JSON"


# ------------------------------------------------------------------ rules
def test_rules_endpoint_strips_internal_notes(server):
    status, payload = request(server, "GET", "/v1/rules")
    assert status == 200
    assert "internal_notes" not in payload
    assert "Allocation test fixture" not in json.dumps(payload)
    assert payload["turnaround_minutes"] == 15
    assert payload["timezone"] == "Asia/Shanghai"


# -------------------------------------------------------------- schedules
def test_batch_allocation_and_input_order(server):
    body = envelope(
        [
            draft("REQ-HT-101", start="2026-09-20T09:00:00+08:00",
                  end="2026-09-20T09:45:00+08:00", priority=2),
            draft("REQ-HT-102", start="2026-09-20T14:00:00+08:00",
                  end="2026-09-20T14:45:00+08:00", priority=5),
        ],
        schedule_id="lab-schedule-http01",
    )
    status, payload = request(server, "POST", "/v1/schedules", body,
                              expect_status=200)
    assert [r["request_id"] for r in payload["results"]] == [
        "REQ-HT-101",
        "REQ-HT-102",
    ]
    assert payload["summary"] == {
        "received": 2, "allocated": 2, "deferred": 0, "conflict": 0,
        "rejected": 0,
    }


def test_mixed_batch_errors_are_isolated(server):
    body = envelope(
        [
            draft("REQ-HT-201", start="2026-09-21T09:00:00+08:00",
                  end="2026-09-21T09:45:00+08:00"),
            draft("REQ-HT-202", device="NOPE", zone="ZONE-C", operator="OP-101"),
            draft("REQ-HT-203", start="2026-09-21T23:30:00+08:00",
                  end="2026-09-22T00:30:00+08:00"),
            draft("REQ-HT-204", start="2026-09-21T11:00:00+08:00",
                  end="2026-09-21T11:30:00+08:00"),
        ],
        schedule_id="lab-schedule-http02",
    )
    status, payload = request(server, "POST", "/v1/schedules", body,
                              expect_status=200)
    by = {r["request_id"]: r for r in payload["results"]}
    assert by["REQ-HT-201"]["decision"] == "allocated"
    assert by["REQ-HT-202"]["decision"] == "rejected"
    assert by["REQ-HT-202"]["error"]["code"] == "UNKNOWN_DEVICE"
    assert by["REQ-HT-203"]["decision"] == "rejected"
    assert by["REQ-HT-203"]["error"]["code"] == "CROSSES_MIDNIGHT"
    assert by["REQ-HT-204"]["decision"] == "allocated"
    assert payload["summary"]["allocated"] == 2
    assert payload["summary"]["rejected"] == 2


def test_maintenance_conflict_over_http(server):
    body = envelope(
        [draft("REQ-HT-301", start="2026-09-09T12:15:00+08:00",
               end="2026-09-09T12:45:00+08:00")],
        schedule_id="lab-schedule-http03",
    )
    _, payload = request(server, "POST", "/v1/schedules", body)
    rec = payload["results"][0]
    assert rec["decision"] == "conflict"
    assert rec["conflict_code"] == "MAINTENANCE_WINDOW"
    assert rec["conflicting_resources"] == ["device:ARM-07"]
    assert rec["next_available_at"] == "2026-09-09T13:00:00+08:00"


def test_envelope_timezone_error_is_400(server):
    body = envelope([draft("REQ-HT-401")])
    body["timezone"] = "Asia/Tokyo"
    status, payload = request(server, "POST", "/v1/schedules", body,
                              expect_status=400)
    assert payload["error"]["code"] == "BAD_TIMEZONE"


# -------------------------------------------------------------- timelines
def test_schedule_timeline_endpoint(server):
    body = envelope(
        [draft("REQ-HT-501", start="2026-09-22T09:00:00+08:00",
               end="2026-09-22T09:30:00+08:00", charging=30)],
        schedule_id="lab-schedule-http04",
    )
    request(server, "POST", "/v1/schedules", body)
    _, payload = request(
        server, "GET", "/v1/schedules/lab-schedule-http04/timeline"
    )
    device_line = payload["timelines"]["device:ARM-07"]
    booking = next(e for e in device_line if e["request_id"] == "REQ-HT-501")
    assert booking["activity_ends_at"] == "2026-09-22T09:30:00+08:00"
    assert booking["ends_at"] == "2026-09-22T10:00:00+08:00"  # 30 min tail


def test_single_resource_timeline_endpoint(server):
    status, payload = request(
        server,
        "GET",
        "/v1/resources/device/ARM-07/timeline?schedule_id=lab-schedule-http04",
        expect_status=200,
    )
    assert payload["resource_type"] == "device"
    assert payload["resource_id"] == "ARM-07"
    assert any(e["request_id"] == "REQ-HT-501" for e in payload["entries"])


def test_timeline_unknown_schedule_is_404(server):
    request(server, "GET", "/v1/schedules/does-not-exist/timeline",
            expect_status=404)


# ---------------------------------------------------------------- explain
def test_explain_reports_single_conflict(server):
    context = [
        draft("REQ-CTX-001", start="2026-09-23T09:00:00+08:00",
              end="2026-09-23T10:00:00+08:00", charging=0),
    ]
    target = draft("REQ-CTX-002", start="2026-09-23T10:10:00+08:00",
                   end="2026-09-23T10:40:00+08:00")
    status, payload = request(
        server, "POST", "/v1/requests/explain",
        {"request": target, "context": context}, expect_status=200,
    )
    assert payload["decision"] == "deferred"
    assert payload["conflict_code"] == "TURNAROUND_REQUIRED"
    assert payload["blocking_request_ids"] == ["REQ-CTX-001"]
    assert payload["next_available_at"] == "2026-09-23T10:15:00+08:00"
    assert payload["required_clear_until"] == "2026-09-23T10:55:00+08:00"


def test_explain_allocated_when_clear(server):
    target = draft("REQ-CTX-003", start="2026-09-24T09:00:00+08:00",
                   end="2026-09-24T09:30:00+08:00")
    _, payload = request(
        server, "POST", "/v1/requests/explain",
        {"request": target, "context": []}, expect_status=200,
    )
    assert payload["decision"] == "allocated"
    assert payload["all_checks"] == []


def test_explain_against_stored_schedule(server):
    _, payload = request(
        server,
        "POST",
        "/v1/requests/explain",
        {
            "request": draft("REQ-CTX-004",
                             start="2026-09-22T09:10:00+08:00",
                             end="2026-09-22T09:40:00+08:00"),
            "context_schedule_id": "lab-schedule-http04",
        },
        expect_status=200,
    )
    assert payload["decision"] == "conflict"
    assert payload["blocking_request_ids"] == ["REQ-HT-501"]


def test_explain_invalid_target_returns_rejected(server):
    _, payload = request(
        server, "POST", "/v1/requests/explain",
        {"request": draft("REQ-CTX-005", device="GHOST", zone="ZONE-C",
                          operator="OP-101")},
        expect_status=200,
    )
    assert payload["decision"] == "rejected"
    assert payload["errors"][0]["code"] == "UNKNOWN_DEVICE"
