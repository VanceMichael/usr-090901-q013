"""引擎语义测试：容量、维护、周转间隔、优先级、跨午夜、下一可用。"""

from __future__ import annotations

from app.service import process_schedule, schedule_response


def decide(loader, helpers, requests, sid="eng-schedule-0001"):
    schedule = process_schedule(helpers.make_schedule(requests, sid), loader)
    return {r["request_id"]: r for r in schedule_response(schedule)["results"]}


# ---------------- 资源容量 ----------------

def test_zone_capacity_two_ok_third_deferred(custom_rules_loader, helpers):
    loader = custom_rules_loader(
        devices=[
            {"device_id": "ARM-07", "allowed_zones": ["ZONE-C", "ZONE-D"], "maintenance": []},
            {"device_id": "ROVER-02", "allowed_zones": ["ZONE-D"], "maintenance": []},
            {"device_id": "ARM-08", "allowed_zones": ["ZONE-D"], "maintenance": []},
        ],
        operators=[
            {"operator_id": "OP-101", "devices": ["ARM-07", "ARM-08"]},
            {"operator_id": "OP-202", "devices": ["ARM-07", "ROVER-02", "ARM-08"]},
        ],
    )
    reqs = [
        helpers.make_req("REQ-D01", device_id="ARM-07", zone_id="ZONE-D",
                         operator_id="OP-101",
                         starts_at="2026-09-09T09:00:00+08:00", ends_at="2026-09-09T10:00:00+08:00", priority=3),
        helpers.make_req("REQ-D02", device_id="ROVER-02", zone_id="ZONE-D",
                         operator_id="OP-202",
                         starts_at="2026-09-09T09:15:00+08:00", ends_at="2026-09-09T10:15:00+08:00", priority=3),
        helpers.make_req("REQ-D03", device_id="ARM-08", zone_id="ZONE-D",
                         operator_id="OP-202",
                         starts_at="2026-09-09T09:30:00+08:00", ends_at="2026-09-09T10:30:00+08:00", priority=3),
    ]
    results = decide(loader, helpers, reqs)
    assert results["REQ-D01"]["decision"] == "allocated"
    assert results["REQ-D02"]["decision"] == "allocated"
    assert results["REQ-D03"]["decision"] == "deferred"
    zone_conflicts = [c for c in results["REQ-D03"]["conflicts"] if c["code"] == "ZONE_CAPACITY"]
    assert len(zone_conflicts) == 1
    assert set(zone_conflicts[0]["blocking_request_ids"]) == {"REQ-D01", "REQ-D02"}


def test_zone_capacity_frees_after_end(custom_rules_loader, helpers):
    loader = custom_rules_loader(
        devices=[
            {"device_id": "DEV-A", "allowed_zones": ["ZONE-D"], "maintenance": []},
            {"device_id": "DEV-B", "allowed_zones": ["ZONE-D"], "maintenance": []},
            {"device_id": "DEV-C", "allowed_zones": ["ZONE-D"], "maintenance": []},
        ],
        operators=[{"operator_id": "OP-X", "devices": ["DEV-A", "DEV-B", "DEV-C"]},
                   {"operator_id": "OP-Y", "devices": ["DEV-A", "DEV-B", "DEV-C"]}],
    )
    reqs = [
        helpers.make_req("REQ-E01", device_id="DEV-A", zone_id="ZONE-D", operator_id="OP-X",
                         starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00"),
        helpers.make_req("REQ-E02", device_id="DEV-B", zone_id="ZONE-D", operator_id="OP-Y",
                         starts_at="2026-09-09T09:10:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00"),
        helpers.make_req("REQ-E03", device_id="DEV-C", zone_id="ZONE-D", operator_id="OP-X",
                         starts_at="2026-09-09T09:30:00+08:00",
                         ends_at="2026-09-09T10:30:00+08:00"),
        # 10:15：前两个预约缓冲恰好结束（10:00+15），容量释放
        helpers.make_req("REQ-E04", device_id="DEV-C", zone_id="ZONE-D", operator_id="OP-Y",
                         starts_at="2026-09-09T10:15:00+08:00",
                         ends_at="2026-09-09T10:45:00+08:00"),
    ]
    results = decide(loader, helpers, reqs)
    # 09:30 第三个到达时前两个仍在（容量 2） → deferred
    assert results["REQ-E03"]["decision"] == "deferred"
    # 10:15 边界：前约缓冲结束、区域有空位 → allocated
    assert results["REQ-E04"]["decision"] == "allocated"


# ---------------- 维护窗口 ----------------

def test_maintenance_overlap_is_conflict(rules_loader, helpers):
    req = helpers.make_req(
        "REQ-MNT1", starts_at="2026-09-09T12:00:00+08:00",
        ends_at="2026-09-09T12:30:00+08:00")
    results = decide(rules_loader, helpers, [req])
    r = results["REQ-MNT1"]
    assert r["decision"] == "conflict"
    codes = {c["code"] for c in r["conflicts"]}
    assert "MAINTENANCE_WINDOW" in codes
    m = next(c for c in r["conflicts"] if c["code"] == "MAINTENANCE_WINDOW")
    assert m["blocking_maintenance"][0]["starts_at"] == "2026-09-09T12:00:00+08:00"
    assert r["next_available_at"] == "2026-09-09T13:00:00+08:00"


def test_buffer_invading_maintenance_conflicts(rules_loader, helpers):
    # 11:50 结束 + 15 分钟周转 = 12:05，侵入维护窗口 → conflict
    req = helpers.make_req(
        "REQ-MNT2", starts_at="2026-09-09T11:30:00+08:00",
        ends_at="2026-09-09T11:50:00+08:00")
    results = decide(rules_loader, helpers, [req])
    assert results["REQ-MNT2"]["decision"] == "conflict"


def test_booking_ending_exactly_at_maintenance_start_ok(rules_loader, helpers):
    # 11:45 + 15 周转 = 12:00，半开区间首尾相接，不打断维护
    req = helpers.make_req(
        "REQ-MNT3", starts_at="2026-09-09T11:30:00+08:00",
        ends_at="2026-09-09T11:45:00+08:00")
    results = decide(rules_loader, helpers, [req])
    assert results["REQ-MNT3"]["decision"] == "allocated"


# ---------------- 准备 / 充电间隔 ----------------

def test_turnaround_required_then_available_at_boundary(rules_loader, helpers):
    reqs = [
        helpers.make_req("REQ-TA1", starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00",
                         charging_minutes_after=20, priority=4),
        helpers.make_req("REQ-TA2", operator_id="OP-202",
                         starts_at="2026-09-09T10:10:00+08:00",
                         ends_at="2026-09-09T10:30:00+08:00", priority=4),
        helpers.make_req("REQ-TA3", operator_id="OP-202",
                         starts_at="2026-09-09T10:20:00+08:00",
                         ends_at="2026-09-09T10:40:00+08:00", priority=4),
    ]
    results = decide(rules_loader, helpers, reqs)
    assert results["REQ-TA1"]["decision"] == "allocated"
    assert results["REQ-TA1"]["buffer_until"] == "2026-09-09T10:20:00+08:00"
    r2 = results["REQ-TA2"]
    assert r2["decision"] == "deferred"
    assert "TURNAROUND_REQUIRED" in {c["code"] for c in r2["conflicts"]}
    assert r2["blocking_request_ids"] == ["REQ-TA1"]
    # 10:20 边界：前约缓冲恰好结束 → 可分配
    assert results["REQ-TA3"]["decision"] == "allocated"


def test_default_turnaround_15_applied(rules_loader, helpers):
    reqs = [
        helpers.make_req("REQ-TB1", starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00"),
        helpers.make_req("REQ-TB2", operator_id="OP-202",
                         starts_at="2026-09-09T10:10:00+08:00",
                         ends_at="2026-09-09T10:30:00+08:00"),
    ]
    results = decide(rules_loader, helpers, reqs)
    assert results["REQ-TB1"]["decision"] == "allocated"
    assert results["REQ-TB2"]["decision"] == "deferred"
    assert results["REQ-TB2"]["next_available_at"] == "2026-09-09T10:15:00+08:00"


# ---------------- 优先级 / 平局 ----------------

def test_higher_priority_preempts_regardless_of_start(rules_loader, helpers):
    reqs = [
        helpers.make_req("REQ-PR1", operator_id="OP-101",
                         starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T10:30:00+08:00", priority=3),
        helpers.make_req("REQ-PR2", operator_id="OP-202",
                         starts_at="2026-09-09T09:30:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00", priority=4),
    ]
    results = decide(rules_loader, helpers, reqs)
    assert results["REQ-PR2"]["decision"] == "allocated"
    assert results["REQ-PR1"]["decision"] == "deferred"
    assert results["REQ-PR1"]["blocking_request_ids"] == ["REQ-PR2"]


def test_same_priority_earlier_start_wins(rules_loader, helpers):
    reqs = [
        helpers.make_req("REQ-PS1", operator_id="OP-202",
                         starts_at="2026-09-09T09:30:00+08:00",
                         ends_at="2026-09-09T10:30:00+08:00", priority=3),
        helpers.make_req("REQ-PS2", operator_id="OP-101",
                         starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00", priority=3),
    ]
    results = decide(rules_loader, helpers, reqs)
    assert results["REQ-PS2"]["decision"] == "allocated"
    assert results["REQ-PS1"]["decision"] == "deferred"


def test_full_tie_broken_by_request_id(rules_loader, helpers):
    reqs = [
        helpers.make_req("REQ-ZZZ", operator_id="OP-202",
                         starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00", priority=3),
        helpers.make_req("REQ-AAA", operator_id="OP-101",
                         starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T10:00:00+08:00", priority=3),
    ]
    results = decide(rules_loader, helpers, reqs)
    assert results["REQ-AAA"]["decision"] == "allocated"
    assert results["REQ-ZZZ"]["decision"] == "deferred"


# ---------------- 跨午夜 ----------------

def test_cross_midnight_rejected(rules_loader, helpers):
    req = helpers.make_req(
        "REQ-CM01", starts_at="2026-09-09T23:30:00+08:00",
        ends_at="2026-09-10T00:30:00+08:00")
    results = decide(rules_loader, helpers, [req])
    assert results["REQ-CM01"]["decision"] == "error"
    assert results["REQ-CM01"]["error"]["code"] == "CROSSES_MIDNIGHT"


def test_late_evening_same_day_ok(rules_loader, helpers):
    req = helpers.make_req(
        "REQ-CM02", starts_at="2026-09-09T23:30:00+08:00",
        ends_at="2026-09-09T23:59:00+08:00")
    results = decide(rules_loader, helpers, [req])
    assert results["REQ-CM02"]["decision"] == "allocated"


# ---------------- 兼容性 / 授权 ----------------

def test_device_zone_incompatible(rules_loader, helpers):
    req = helpers.make_req("REQ-IZ01", device_id="ROVER-02", zone_id="ZONE-C",
                           operator_id="OP-202")
    results = decide(rules_loader, helpers, [req])
    assert results["REQ-IZ01"]["error"]["code"] == "DEVICE_ZONE_INCOMPATIBLE"


def test_operator_not_authorized(rules_loader, helpers):
    req = helpers.make_req("REQ-IA01", device_id="ROVER-02", zone_id="ZONE-D",
                           operator_id="OP-101")
    results = decide(rules_loader, helpers, [req])
    assert results["REQ-IA01"]["error"]["code"] == "OPERATOR_NOT_AUTHORIZED"


# ---------------- 下一可用：多资源取最晚 ----------------

def test_next_available_waits_for_operator(rules_loader, helpers):
    reqs = [
        # OP-202 先用 ARM-07 在 ZONE-C 忙到 11:00
        helpers.make_req("REQ-OP1", operator_id="OP-202",
                         starts_at="2026-09-09T09:00:00+08:00",
                         ends_at="2026-09-09T11:00:00+08:00", priority=4),
        # ROVER-02 与 ZONE-D 空闲，但 OP-202 忙
        helpers.make_req("REQ-OP2", device_id="ROVER-02", zone_id="ZONE-D",
                         operator_id="OP-202",
                         starts_at="2026-09-09T10:00:00+08:00",
                         ends_at="2026-09-09T10:30:00+08:00", priority=4),
    ]
    results = decide(rules_loader, helpers, reqs)
    r2 = results["REQ-OP2"]
    assert r2["decision"] == "deferred"
    assert {(c["resource_type"], c["code"]) for c in r2["conflicts"]} == {
        ("operator", "OPERATOR_OVERLAP")
    }
    # 11:00 + 前约 15 分钟周转 → 11:15
    assert r2["next_available_at"] == "2026-09-09T11:15:00+08:00"


def test_next_available_fixed_point_across_later_maintenance_free_booking(custom_rules_loader, helpers):
    # FX2（高优先级、另一设备但同一指导员）先分配：13:30-14:30 占用 OP-101。
    # FX1 被设备维护推到 13:00，但该时刻指导员仍忙 → 综合下一可用必须迭代到 14:45，
    # 而不是只取第一轮各资源建议最大值得到的 13:00。
    loader = custom_rules_loader(
        operators=[{"operator_id": "OP-101", "devices": ["ARM-07", "ROVER-02"]},
                   {"operator_id": "OP-202", "devices": ["ARM-07", "ROVER-02"]}],
    )
    reqs = [
        helpers.make_req("REQ-FX1",
                         starts_at="2026-09-09T12:20:00+08:00",
                         ends_at="2026-09-09T12:40:00+08:00", priority=3),
        helpers.make_req("REQ-FX2", device_id="ROVER-02", zone_id="ZONE-D",
                         operator_id="OP-101",
                         starts_at="2026-09-09T13:30:00+08:00",
                         ends_at="2026-09-09T14:30:00+08:00", priority=5),
    ]
    results = decide(loader, helpers, reqs)
    fx1 = results["REQ-FX1"]
    assert fx1["decision"] == "conflict"
    assert fx1["next_available_at"] == "2026-09-09T14:45:00+08:00"


def test_sample_fixture_schedules_both_requests(rules_loader, helpers):
    import json
    from pathlib import Path
    samples = json.loads(
        (Path(__file__).resolve().parent.parent / "fixtures" / "sample-requests.json")
        .read_text(encoding="utf-8"))
    payload = samples[0]
    schedule = process_schedule(payload, rules_loader)
    body = schedule_response(schedule)
    decisions = {r["request_id"]: r["decision"] for r in body["results"]}
    assert decisions == {"REQ-ARM-301": "allocated", "REQ-ROVER-302": "allocated"}
