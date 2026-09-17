#!/usr/bin/env python3
"""容器 HTTP 黑盒自测：对运行中的排程服务做端到端断言。

用法：python selftest.py [BASE_URL]，默认 http://api:8080（compose 网络）。
退出码 0 = 全部通过；非 0 = 有用例失败。仅依赖标准库，不连接真实机器人/校园系统。

覆盖：资源容量、维护抢占、准备间隔、优先级平局、跨午夜、混合批错误隔离，
以及时间线、冲突解释、规则脱敏与结构化错误。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://api:8080"
FAILURES: list[str] = []
_SEQ = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" :: {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def sid(prefix: str) -> str:
    global _SEQ
    _SEQ += 1
    return f"self-{prefix}-{_SEQ:03d}"


def req(rid, **over):
    base = {
        "request_id": rid, "device_id": "ARM-07", "zone_id": "ZONE-C",
        "operator_id": "OP-101",
        "starts_at": "2026-09-09T09:00:00+08:00",
        "ends_at": "2026-09-09T10:00:00+08:00",
        "priority": 3,
    }
    base.update(over)
    return base


def schedule(schedule_id, requests):
    status, body = call("POST", "/schedules",
                        {"schedule_id": schedule_id, "timezone": "Asia/Shanghai",
                         "requests": requests})
    assert status == 200, (schedule_id, body)
    return {r["request_id"]: r for r in body["results"]}


def main() -> int:
    # 0. 探活
    status, body = call("GET", "/healthz")
    check("healthz 200", status == 200 and body.get("status") == "ok", str(body))

    # 0b. 规则脱敏
    status, rules = call("GET", "/rules")
    raw = json.dumps(rules, ensure_ascii=False)
    check("规则查询返回周转与顺序",
          status == 200 and rules["turnaround_minutes"] == 15
          and rules["allocation_order"] == ["priority_desc", "starts_at_asc", "request_id_asc"])
    check("规则不泄露 internal_notes", "internal_notes" not in raw and "Allocation test fixture" not in raw)

    # 1. 资源容量：ZONE-D 容量 2，不同设备/指导员的 2 个并发预约占满，第 3 个被容量拒绝
    r = schedule(sid("cap"), [
        req("REQ-CAPA", device_id="ARM-07", zone_id="ZONE-D", operator_id="OP-101",
            starts_at="2026-09-09T09:00:00+08:00", ends_at="2026-09-09T10:00:00+08:00"),
        req("REQ-CAPB", device_id="ROVER-02", zone_id="ZONE-D", operator_id="OP-202",
            starts_at="2026-09-09T09:00:00+08:00", ends_at="2026-09-09T10:00:00+08:00"),
        req("REQ-CAPC", device_id="ARM-07", zone_id="ZONE-D", operator_id="OP-202",
            starts_at="2026-09-09T09:30:00+08:00", ends_at="2026-09-09T10:30:00+08:00"),
    ])
    check("容量内 2 个并发预约 allocated",
          r["REQ-CAPA"]["decision"] == "allocated" and r["REQ-CAPB"]["decision"] == "allocated")
    capc_codes = {c["code"] for c in r["REQ-CAPC"]["conflicts"]}
    check("第 3 个并发命中 ZONE_CAPACITY 且阻塞者正确",
          r["REQ-CAPC"]["decision"] == "deferred" and "ZONE_CAPACITY" in capc_codes
          and set(r["REQ-CAPC"]["blocking_request_ids"]) == {"REQ-CAPA", "REQ-CAPB"},
          str(capc_codes))

    # 2. 维护抢占：12:00-13:00 维护不可打断
    r = schedule(sid("mnt"), [
        req("REQ-MNTX", starts_at="2026-09-09T12:00:00+08:00",
            ends_at="2026-09-09T12:30:00+08:00"),
    ])
    codes = {c["code"] for c in r["REQ-MNTX"]["conflicts"]}
    check("维护重叠判 conflict 且不分配",
          r["REQ-MNTX"]["decision"] == "conflict" and "MAINTENANCE_WINDOW" in codes, str(codes))
    check("下一可用时段为维护结束 13:00",
          r["REQ-MNTX"]["next_available_at"] == "2026-09-09T13:00:00+08:00",
          r["REQ-MNTX"]["next_available_at"])
    # 缓冲侵入维护也算冲突；恰好 11:45 结束（+15 周转=12:00）可排
    r = schedule(sid("mnt2"), [
        req("REQ-MNTY", starts_at="2026-09-09T11:30:00+08:00",
            ends_at="2026-09-09T11:50:00+08:00"),
        req("REQ-MNTZ", starts_at="2026-09-09T11:30:00+08:00",
            ends_at="2026-09-09T11:45:00+08:00", operator_id="OP-202"),
    ])
    check("缓冲侵入维护判 conflict", r["REQ-MNTY"]["decision"] == "conflict")
    check("首尾相接不打断维护可 allocated", r["REQ-MNTZ"]["decision"] == "allocated")

    # 3. 准备/充电间隔
    r = schedule(sid("trn"), [
        req("REQ-TRN1", starts_at="2026-09-09T09:00:00+08:00",
            ends_at="2026-09-09T10:00:00+08:00", charging_minutes_after=20, priority=4),
        req("REQ-TRN2", operator_id="OP-202",
            starts_at="2026-09-09T10:10:00+08:00", ends_at="2026-09-09T10:30:00+08:00"),
        req("REQ-TRN3", operator_id="OP-202",
            starts_at="2026-09-09T10:20:00+08:00", ends_at="2026-09-09T10:40:00+08:00"),
    ])
    tcodes = {c["code"] for c in r["REQ-TRN2"]["conflicts"]}
    check("间隔不足判 deferred/TURNAROUND_REQUIRED 且阻塞者为前约",
          r["REQ-TRN2"]["decision"] == "deferred" and "TURNAROUND_REQUIRED" in tcodes
          and r["REQ-TRN2"]["blocking_request_ids"] == ["REQ-TRN1"], str(tcodes))
    check("间隔边界 10:20 可排", r["REQ-TRN3"]["decision"] == "allocated")

    # 4. 优先级平局：(priority desc, starts_at asc, request_id asc)
    r = schedule(sid("prio"), [
        req("REQ-LOWX", operator_id="OP-101", priority=3,
            starts_at="2026-09-09T09:00:00+08:00", ends_at="2026-09-09T10:30:00+08:00"),
        req("REQ-HIGH", operator_id="OP-202", priority=5,
            starts_at="2026-09-09T09:30:00+08:00", ends_at="2026-09-09T10:00:00+08:00"),
    ])
    check("高优先级抢占",
          r["REQ-HIGH"]["decision"] == "allocated" and r["REQ-LOWX"]["decision"] == "deferred"
          and r["REQ-LOWX"]["blocking_request_ids"] == ["REQ-HIGH"])
    r = schedule(sid("tie"), [
        req("REQ-TIEZ", operator_id="OP-202",
            starts_at="2026-09-09T09:00:00+08:00", ends_at="2026-09-09T10:00:00+08:00"),
        req("REQ-TIEA", operator_id="OP-101",
            starts_at="2026-09-09T09:00:00+08:00", ends_at="2026-09-09T10:00:00+08:00"),
    ])
    check("完全平局按 request_id 字典序",
          r["REQ-TIEA"]["decision"] == "allocated" and r["REQ-TIEZ"]["decision"] == "deferred")

    # 5. 跨午夜
    r = schedule(sid("mid"), [
        req("REQ-MIDX", starts_at="2026-09-09T23:40:00+08:00",
            ends_at="2026-09-10T00:20:00+08:00"),
    ])
    check("跨午夜稳定结构化错误",
          r["REQ-MIDX"]["decision"] == "error"
          and r["REQ-MIDX"]["error"]["code"] == "CROSSES_MIDNIGHT",
          str(r["REQ-MIDX"].get("error")))

    # 6. 混合批：错误隔离、输入保序、非法信封、未知设备、非法时区时间戳
    good1 = {"schedule_id": sid("bg1"), "timezone": "Asia/Shanghai", "requests": [
        req("REQ-BG1X", starts_at="2026-09-09T08:00:00+08:00",
            ends_at="2026-09-09T08:30:00+08:00")]}
    bad_envelope = {"schedule_id": "x", "timezone": "Asia/Shanghai", "requests": []}
    mixed = {"schedule_id": sid("mix"), "timezone": "Asia/Shanghai", "requests": [
        req("REQ-MIX1", starts_at="2026-09-09T08:00:00+08:00",
            ends_at="2026-09-09T08:30:00+08:00"),
        req("REQ-MIX2", device_id="GHOST-9"),
        req("REQ-MIX3", starts_at="2026-09-09T12:10:00+08:00",
            ends_at="2026-09-09T12:50:00+08:00", priority=5),
        req("REQ-MIX4", starts_at="2026-09-09T23:40:00+08:00",
            ends_at="2026-09-10T00:20:00+08:00"),
        req("REQ-MIX5", starts_at="2026-09-09T09:00:00Z",
            ends_at="2026-09-09T10:00:00+08:00"),
        "not-an-object",
    ]}
    good2 = {"schedule_id": sid("bg2"), "timezone": "Asia/Shanghai", "requests": [
        req("REQ-BG2X", device_id="ROVER-02", zone_id="ZONE-D", operator_id="OP-202",
            starts_at="2026-09-09T15:00:00+08:00",
            ends_at="2026-09-09T15:30:00+08:00")]}
    status, body = call("POST", "/schedules/batch",
                        {"schedules": [good1, bad_envelope, mixed, good2]})
    entries = body["schedules"]
    check("批量 HTTP 200 且保序",
          status == 200 and [e["input_index"] for e in entries] == [0, 1, 2, 3], str(body)[:200])
    check("坏信封不影响其他合法排程",
          entries[0]["status"] == "processed" and entries[1]["error"]["code"] == "SCHEMA_VIOLATION"
          and entries[3]["status"] == "processed")
    mr = entries[2]["result"]["results"]
    by = {x["request_id"]: x for x in mr}
    check("混合批内结果保序",
          [x["request_id"] for x in mr] ==
          ["REQ-MIX1", "REQ-MIX2", "REQ-MIX3", "REQ-MIX4", "REQ-MIX5", "UNKNOWN-5"])
    check("未知设备稳定错误且不影响同批合法预约",
          by["REQ-MIX1"]["decision"] == "allocated"
          and by["REQ-MIX2"]["decision"] == "error"
          and by["REQ-MIX2"]["error"]["code"] == "UNKNOWN_DEVICE")
    check("维护冲突在混合批中仍判 conflict",
          by["REQ-MIX3"]["decision"] == "conflict"
          and by["REQ-MIX3"]["next_available_at"] == "2026-09-09T13:00:00+08:00")
    check("跨午夜与非法时区时间戳分别给稳定错误",
          by["REQ-MIX4"]["error"]["code"] == "CROSSES_MIDNIGHT"
          and by["REQ-MIX5"]["error"]["code"] == "INVALID_TIMEZONE")
    check("非对象元素给 SCHEMA_VIOLATION", mr[5]["error"]["code"] == "SCHEMA_VIOLATION")

    # 非法时区信封
    status, body = call("POST", "/schedules", {
        "schedule_id": sid("tz"), "timezone": "UTC", "requests": [req("REQ-TZBAD")]})
    check("非法时区信封被整体拒绝",
          status == 400 and body["error"]["code"] == "SCHEMA_VIOLATION")

    # 7. 时间线 + 单预约解释
    schedule_id = sid("tl")
    call("POST", "/schedules", {"schedule_id": schedule_id, "timezone": "Asia/Shanghai",
                                 "requests": [
        req("REQ-TL1X", priority=5, starts_at="2026-09-09T09:00:00+08:00",
            ends_at="2026-09-09T10:30:00+08:00"),
        req("REQ-TL2X", operator_id="OP-202",
            starts_at="2026-09-09T09:30:00+08:00", ends_at="2026-09-09T10:00:00+08:00")]})
    status, body = call("GET", f"/schedules/{schedule_id}/timeline?resource_type=device&resource_id=ARM-07")
    kinds = [(i["kind"], i.get("request_id")) for i in body["timeline"]]
    check("设备时间线含维护与已分配且按时间排序",
          status == 200 and ("maintenance", None) in kinds
          and ("allocated", "REQ-TL1X") in kinds
          and [i["starts_at"] for i in body["timeline"]]
          == sorted(i["starts_at"] for i in body["timeline"]))
    status, body = call("GET", f"/schedules/{schedule_id}/requests/REQ-TL2X")
    check("单预约冲突解释含决策、阻塞预约与下一可用",
          status == 200 and body["decision"] == "deferred"
          and "REQ-TL1X" in body["blocking_request_ids"]
          and any("REQ-TL1X" in line for line in body["explanation"]))

    print()
    if FAILURES:
        print(f"SELF-TEST FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("SELF-TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
