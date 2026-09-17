#!/usr/bin/env python3
"""Black-box HTTP self-test, meant to run *inside* a container network.

It exercises the running scheduler over real HTTP (no imports from the app
package) and covers every scenario the coordinator requires:

  1. resource capacity            (capacity-2 zone, third group rejected)
  2. maintenance preemption       (overlap and tail-into-window)
  3. preparation/charging gap     (deferred + next available time)
  4. priority tie-break           (priority / start / request_id ordering)
  5. cross-midnight scheduling    (rejected, next-midnight start accepted)
  6. mixed batch errors           (bad drafts isolated, good ones unaffected)

plus rules hygiene (internal_notes never leaks), timelines and the single
request explanation endpoint.

Configure with SCHEDULER_URL and SCHEDULER_EXTENDED_URL. Exits non-zero on
the first failed assertion.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SCHEDULER_URL", "http://127.0.0.1:8080").rstrip("/")
BASE_EXT = os.environ.get(
    "SCHEDULER_EXTENDED_URL", BASE
).rstrip("/")

CHECKS = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = None) -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}" + (f"  -> {json.dumps(detail, ensure_ascii=False)}"
                                   if detail is not None else ""))


def call(method: str, url: str, body: object | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_for(url: str, attempts: int = 30) -> None:
    for _ in range(attempts):
        try:
            status, _ = call("GET", url + "/healthz")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise SystemExit(f"scheduler not healthy at {url}")


def draft(request_id, *, device="ARM-07", zone="ZONE-C", operator="OP-101",
          start, end, priority=3, charging=None):
    body = {
        "request_id": request_id,
        "device_id": device,
        "zone_id": zone,
        "operator_id": operator,
        "starts_at": start,
        "ends_at": end,
        "priority": priority,
    }
    if charging is not None:
        body["charging_minutes_after"] = charging
    return body


def schedule(base, schedule_id, requests):
    status, payload = call(
        "POST",
        base + "/v1/schedules",
        {"schedule_id": schedule_id, "timezone": "Asia/Shanghai",
         "requests": requests},
    )
    assert status == 200, (schedule_id, payload)
    return payload


def by_id(payload):
    return {r["request_id"]: r for r in payload["results"]}


def main() -> int:
    print(f"waiting for scheduler at {BASE}")
    wait_for(BASE)
    if BASE_EXT != BASE:
        print(f"waiting for extended scheduler at {BASE_EXT}")
        wait_for(BASE_EXT)

    # --------------------------------------------------------------- health
    print("[health]")
    status, payload = call("GET", BASE + "/healthz")
    check("healthz returns ok", status == 200 and payload == {"status": "ok"})

    # ---------------------------------------------------------------- rules
    print("[rules]")
    status, rules = call("GET", BASE + "/v1/rules")
    rules_text = json.dumps(rules, ensure_ascii=False)
    check("rules status 200", status == 200)
    check("rules timezone pinned", rules.get("timezone") == "Asia/Shanghai")
    check("turnaround_minutes from fixture", rules.get("turnaround_minutes") == 15)
    check("internal_notes never leaked",
          "internal_notes" not in rules and "Allocation test fixture" not in rules_text,
          rules)

    # ------------------------------------------------------- 1. capacity
    print("[capacity]")
    cap = schedule(
        BASE_EXT,
        "self-test-capacity1",
        [
            draft("REQ-CAP-001", device="ROVER-02", zone="ZONE-D",
                  operator="OP-202", start="2026-10-01T09:00:00+08:00",
                  end="2026-10-01T10:00:00+08:00"),
            draft("REQ-CAP-002", device="ROVER-03", zone="ZONE-D",
                  operator="OP-303", start="2026-10-01T09:10:00+08:00",
                  end="2026-10-01T09:40:00+08:00"),
            draft("REQ-CAP-003", device="ARM-07", zone="ZONE-D",
                  operator="OP-202", start="2026-10-01T09:20:00+08:00",
                  end="2026-10-01T09:45:00+08:00"),
        ],
    )
    d = by_id(cap)
    check("two simultaneous groups fit capacity-2 zone",
          d["REQ-CAP-001"]["decision"] == "allocated"
          and d["REQ-CAP-002"]["decision"] == "allocated")
    check("third simultaneous group hits ZONE_CAPACITY",
          d["REQ-CAP-003"]["decision"] == "conflict"
          and d["REQ-CAP-003"]["conflict_code"] == "ZONE_CAPACITY"
          and d["REQ-CAP-003"]["conflicting_resources"] == ["zone:ZONE-D"],
          d["REQ-CAP-003"])
    check("capacity conflict names both blocking requests",
          set(d["REQ-CAP-003"]["blocking_request_ids"]) == {"REQ-CAP-001",
                                                            "REQ-CAP-002"})
    check("next_available_at is earliest slot release (09:40)",
          d["REQ-CAP-003"]["next_available_at"] == "2026-10-01T09:40:00+08:00",
          d["REQ-CAP-003"])

    # ---------------------------------------------------- 2. maintenance
    print("[maintenance]")
    mt = schedule(
        BASE,
        "self-test-maint1",
        [
            draft("REQ-MNT-001", start="2026-09-09T12:15:00+08:00",
                  end="2026-09-09T12:45:00+08:00"),
            draft("REQ-MNT-002", start="2026-09-09T11:30:00+08:00",
                  end="2026-09-09T12:00:00+08:00", charging=0),
            draft("REQ-MNT-003", start="2026-09-09T13:00:00+08:00",
                  end="2026-09-09T13:30:00+08:00"),
        ],
    )
    d = by_id(mt)
    check("overlap with maintenance is conflict",
          d["REQ-MNT-001"]["decision"] == "conflict"
          and d["REQ-MNT-001"]["conflict_code"] == "MAINTENANCE_WINDOW"
          and d["REQ-MNT-001"]["conflicting_resources"] == ["device:ARM-07"]
          and d["REQ-MNT-001"]["next_available_at"] == "2026-09-09T13:00:00+08:00",
          d["REQ-MNT-001"])
    check("tail intruding into maintenance is preempted",
          d["REQ-MNT-002"]["decision"] == "conflict"
          and d["REQ-MNT-002"]["conflict_code"] == "MAINTENANCE_WINDOW",
          d["REQ-MNT-002"])
    check("start exactly when maintenance ends is allocated",
          d["REQ-MNT-003"]["decision"] == "allocated", d["REQ-MNT-003"])

    # ------------------------------------------------ 3. turnaround/gap
    print("[turnaround]")
    tr = schedule(
        BASE,
        "self-test-turn1",
        [
            draft("REQ-TRN-001", start="2026-10-02T09:00:00+08:00",
                  end="2026-10-02T10:00:00+08:00", charging=0),
            draft("REQ-TRN-002", start="2026-10-02T10:10:00+08:00",
                  end="2026-10-02T10:40:00+08:00"),
            draft("REQ-TRN-003", start="2026-10-02T10:15:00+08:00",
                  end="2026-10-02T10:45:00+08:00"),
            draft("REQ-TRN-004", start="2026-10-02T09:00:00+08:00",
                  end="2026-10-02T09:30:00+08:00", charging=45,
                  device="ROVER-02", zone="ZONE-D", operator="OP-202"),
            draft("REQ-TRN-005", start="2026-10-02T10:00:00+08:00",
                  end="2026-10-02T10:20:00+08:00",
                  device="ROVER-02", zone="ZONE-D", operator="OP-202"),
        ],
    )
    d = by_id(tr)
    check("10-minute gap is deferred with TURNAROUND_REQUIRED",
          d["REQ-TRN-002"]["decision"] == "deferred"
          and d["REQ-TRN-002"]["conflict_code"] == "TURNAROUND_REQUIRED"
          and d["REQ-TRN-002"]["next_available_at"] == "2026-10-02T10:15:00+08:00"
          and "device:ARM-07" in d["REQ-TRN-002"]["conflicting_resources"]
          and "operator:OP-101" in d["REQ-TRN-002"]["conflicting_resources"]
          and d["REQ-TRN-002"]["blocking_request_ids"] == ["REQ-TRN-001"],
          d["REQ-TRN-002"])
    check("exact 15-minute gap is allocated",
          d["REQ-TRN-003"]["decision"] == "allocated", d["REQ-TRN-003"])
    check("45-minute charging extends the blocked tail",
          d["REQ-TRN-005"]["decision"] == "deferred"
          and d["REQ-TRN-005"]["next_available_at"] == "2026-10-02T10:15:00+08:00",
          d["REQ-TRN-005"])

    # ------------------------------------------------- 4. priority ties
    print("[priority]")
    pr = schedule(
        BASE,
        "self-test-prio1",
        [
            draft("REQ-PRI-001", start="2026-10-03T09:00:00+08:00",
                  end="2026-10-03T10:00:00+08:00", priority=1),
            draft("REQ-PRI-002", start="2026-10-03T09:30:00+08:00",
                  end="2026-10-03T10:00:00+08:00", priority=5),
            draft("REQ-PRI-004", start="2026-10-03T11:00:00+08:00",
                  end="2026-10-03T11:30:00+08:00", priority=3),
            draft("REQ-PRI-003", start="2026-10-03T11:00:00+08:00",
                  end="2026-10-03T11:30:00+08:00", priority=3),
        ],
    )
    d = by_id(pr)
    check("higher priority wins over earlier start",
          d["REQ-PRI-002"]["decision"] == "allocated"
          and d["REQ-PRI-001"]["decision"] == "conflict"
          and d["REQ-PRI-001"]["blocking_request_ids"] == ["REQ-PRI-002"],
          [d["REQ-PRI-001"], d["REQ-PRI-002"]])
    check("request_id asc breaks the final tie",
          d["REQ-PRI-003"]["decision"] == "allocated"
          and d["REQ-PRI-004"]["decision"] == "conflict"
          and d["REQ-PRI-004"]["blocking_request_ids"] == ["REQ-PRI-003"],
          [d["REQ-PRI-003"], d["REQ-PRI-004"]])
    check("batch results preserve input order",
          [r["request_id"] for r in pr["results"]]
          == ["REQ-PRI-001", "REQ-PRI-002", "REQ-PRI-004", "REQ-PRI-003"])

    # ---------------------------------------------- 5. cross-midnight
    print("[cross-midnight]")
    cm = schedule(
        BASE,
        "self-test-mid1",
        [
            draft("REQ-MID-001", start="2026-10-04T23:30:00+08:00",
                  end="2026-10-05T00:30:00+08:00"),
            draft("REQ-MID-002", start="2026-10-05T00:00:00+08:00",
                  end="2026-10-05T00:30:00+08:00"),
        ],
    )
    d = by_id(cm)
    check("cross-midnight draft is structurally rejected",
          d["REQ-MID-001"]["decision"] == "rejected"
          and d["REQ-MID-001"]["error"]["code"] == "CROSSES_MIDNIGHT",
          d["REQ-MID-001"])
    check("booking starting exactly at midnight next day is allocated",
          d["REQ-MID-002"]["decision"] == "allocated", d["REQ-MID-002"])

    # ------------------------------------------ 6. mixed batch errors
    print("[mixed-batch]")
    mb_status, mb = call(
        "POST",
        BASE + "/v1/schedules",
        {
            "schedule_id": "self-test-mixed1",
            "timezone": "Asia/Shanghai",
            "requests": [
                draft("REQ-MIX-001", start="2026-10-06T09:00:00+08:00",
                      end="2026-10-06T09:45:00+08:00", priority=4),
                draft("REQ-MIX-002", device="GHOST-9", zone="ZONE-C",
                      operator="OP-101", start="2026-10-06T10:00:00+08:00",
                      end="2026-10-06T10:30:00+08:00"),
                "not-an-object",
                draft("REQ-MIX-004", start="2026-10-06T15:00:00+08:00",
                      end="2026-10-06T15:30:00+08:00"),
                draft("REQ-MIX-005", start="2026-10-06T16:00:00+08:00",
                      end="2026-10-06T16:30:00+08:00"),
            ],
        },
    )
    d = by_id(mb)
    check("mixed batch returns 200 (per-item isolation)", mb_status == 200)
    check("valid requests around bad items still allocate",
          d["REQ-MIX-001"]["decision"] == "allocated"
          and d["REQ-MIX-004"]["decision"] == "allocated"
          and d["REQ-MIX-005"]["decision"] == "allocated", mb["results"])
    check("unknown device is a stable structured error",
          d["REQ-MIX-002"]["decision"] == "rejected"
          and d["REQ-MIX-002"]["error"]["code"] == "UNKNOWN_DEVICE",
          d["REQ-MIX-002"])
    check("non-object item is a stable structured error",
          mb["results"][2]["decision"] == "rejected"
          and mb["results"][2]["error"]["code"] == "NOT_OBJECT")
    check("input indexes retained",
          [r["index"] for r in mb["results"]] == [0, 1, 2, 3, 4])
    check("summary counts are consistent",
          mb["summary"] == {"received": 5, "allocated": 3, "deferred": 0,
                            "conflict": 0, "rejected": 2}, mb["summary"])

    # Envelope-level illegal timezone must be a structured 400.
    bad_tz_status, bad_tz = call(
        "POST",
        BASE + "/v1/schedules",
        {"schedule_id": "self-test-badtz01", "timezone": "UTC",
         "requests": [draft("REQ-TZ-001", start="2026-10-07T09:00:00+08:00",
                            end="2026-10-07T09:30:00+08:00")]},
    )
    check("illegal timezone is a structured 400",
          bad_tz_status == 400 and bad_tz["error"]["code"] == "BAD_TIMEZONE",
          bad_tz)
    wrong_method_status, wrong_method = call("GET", BASE + "/v1/schedules")
    check("GET on POST-only route is a structured 405",
          wrong_method_status == 405
          and wrong_method["error"]["code"] == "METHOD_NOT_ALLOWED",
          wrong_method)

    # ------------------------------------------------------------ timeline
    print("[timeline]")
    schedule(
        BASE,
        "self-test-line1",
        [draft("REQ-LIN-001", start="2026-10-08T09:00:00+08:00",
               end="2026-10-08T09:30:00+08:00", charging=30)],
    )
    status, line = call(
        "GET", BASE + "/v1/schedules/self-test-line1/timeline"
        "?resource_type=device&resource_id=ARM-07"
    )
    check("schedule timeline query returns device entries",
          status == 200 and any(
              e["request_id"] == "REQ-LIN-001"
              and e["ends_at"] == "2026-10-08T10:00:00+08:00"
              and e["activity_ends_at"] == "2026-10-08T09:30:00+08:00"
              for e in line["entries"]), line)
    status, rline = call(
        "GET",
        BASE + "/v1/resources/device/ARM-07/timeline?schedule_id=self-test-line1",
    )
    check("resource-centric timeline works",
          status == 200 and rline["resource_id"] == "ARM-07"
          and any(e["request_id"] == "REQ-LIN-001" for e in rline["entries"]),
          rline)
    status, missing = call("GET", BASE + "/v1/schedules/nope/timeline")
    check("unknown schedule timeline is structured 404",
          status == 404 and missing["error"]["code"] == "ROUTE_NOT_FOUND", missing)

    # ------------------------------------------------------------- explain
    print("[explain]")
    status, expl = call(
        "POST",
        BASE + "/v1/requests/explain",
        {
            "request": draft("REQ-EXP-002",
                             start="2026-10-08T09:10:00+08:00",
                             end="2026-10-08T09:40:00+08:00"),
            "context_schedule_id": "self-test-line1",
        },
    )
    check("explain against stored schedule reports the blocker",
          status == 200 and expl["decision"] == "conflict"
          and expl["blocking_request_ids"] == ["REQ-LIN-001"]
          and expl["conflict_code"] == "DEVICE_OVERLAP", expl)

    status, expl2 = call(
        "POST",
        BASE + "/v1/requests/explain",
        {
            "request": draft("REQ-EXP-003",
                             start="2026-10-08T10:10:00+08:00",
                             end="2026-10-08T10:40:00+08:00"),
            "context": [
                draft("REQ-EXP-001", start="2026-10-08T10:00:00+08:00",
                      end="2026-10-08T10:10:00+08:00")
            ],
        },
    )
    check("explain with inline context reports turnaround deferral",
          status == 200 and expl2["decision"] == "deferred"
          and expl2["conflict_code"] == "TURNAROUND_REQUIRED"
          and expl2["next_available_at"] == "2026-10-08T10:25:00+08:00", expl2)

    # -------------------------------------------------------------- report
    print()
    print(f"checks: {CHECKS}, failures: {len(FAILURES)}")
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("ALL HTTP SELF-TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
