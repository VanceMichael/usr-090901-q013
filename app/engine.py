"""排程引擎：校验、固定顺序分配、冲突解释与下一可用时段。

分配顺序（来自 fixtures 的 allocation_order）：
priority_desc -> starts_at_asc -> request_id_asc。
已 allocated 的预约占用资源；deferred/conflict/error 不占用。

间隔模型：每条预约结束后需要一个"准备/充电"缓冲
  buffer = max(rules.turnaround_minutes, request.charging_minutes_after)
同资源上两条预约之间，任一侧缓冲不足即判 TURNAROUND_REQUIRED。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from .errors import (
    CROSSES_MIDNIGHT,
    DEVICE_OVERLAP,
    DEVICE_ZONE_INCOMPATIBLE,
    INVALID_INTERVAL,
    INVALID_TIMESTAMP,
    MAINTENANCE_WINDOW,
    OPERATOR_NOT_AUTHORIZED,
    OPERATOR_OVERLAP,
    TURNAROUND_REQUIRED,
    UNKNOWN_DEVICE,
    UNKNOWN_OPERATOR,
    UNKNOWN_ZONE,
    DECISION_ALLOCATED,
    DECISION_CONFLICT,
    DECISION_DEFERRED,
    ZONE_CAPACITY,
    StructuredError,
)
from .rules import MaintenanceWindow, Rules
from .timeutil import iso, parse_shanghai, same_calendar_day

# 防止异常数据导致下一可用时段搜索不收敛
_MAX_NEXT_ITER = 500


@dataclass
class Booking:
    request_id: str
    device_id: str
    zone_id: str
    operator_id: str
    starts_at: datetime
    ends_at: datetime
    priority: int
    buffer_minutes: int
    input_index: int

    @property
    def block_ends_at(self) -> datetime:
        return self.ends_at + timedelta(minutes=self.buffer_minutes)

    @property
    def duration(self) -> timedelta:
        return self.ends_at - self.starts_at


@dataclass
class RequestResult:
    request_id: str
    decision: str
    input_index: int
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    booking: Booking | None = None
    overall_next: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        blocking = sorted({
            rid
            for c in self.conflicts
            for rid in c.get("blocking_request_ids", [])
        })
        out: dict[str, Any] = {
            "request_id": self.request_id,
            "decision": self.decision,
            "conflicts": self.conflicts,
            "blocking_request_ids": blocking,
            # 三类资源同时空闲的最早时刻（定点迭代结果，由 Schedule 填入）
            "next_available_at": iso(self.overall_next) if self.overall_next else None,
            "allocated_resources": [],
        }
        if self.decision == DECISION_ALLOCATED and self.booking is not None:
            b = self.booking
            out["allocated_resources"] = [
                {"resource_type": "device", "resource_id": b.device_id},
                {"resource_type": "zone", "resource_id": b.zone_id},
                {"resource_type": "operator", "resource_id": b.operator_id},
            ]
            out["starts_at"] = iso(b.starts_at)
            out["ends_at"] = iso(b.ends_at)
            out["buffer_until"] = iso(b.block_ends_at)
        if self.error is not None:
            out["error"] = self.error
        return out


class Schedule:
    """一次排程批次的可变状态。"""

    def __init__(self, schedule_id: str, rules: Rules) -> None:
        self.schedule_id = schedule_id
        self.rules = rules
        self.bookings: list[Booking] = []
        self.results: dict[str, RequestResult] = {}
        # 由服务层填充：按输入位置保存的错误结果、输入顺序 (index, request_id)
        self.error_results: dict[int, Any] = {}
        self.input_order: list[tuple[int, str]] = []

    # ---------- 占用视图 ----------
    def _bookings(self, resource_type: str, resource_id: str) -> list[Booking]:
        if resource_type == "device":
            return [b for b in self.bookings if b.device_id == resource_id]
        if resource_type == "zone":
            return [b for b in self.bookings if b.zone_id == resource_id]
        return [b for b in self.bookings if b.operator_id == resource_id]

    # ---------- 静态语义校验 ----------
    def validate_request(self, req: dict[str, Any]) -> tuple[Booking | None, dict[str, Any] | None]:
        rid = req.get("request_id", "UNKNOWN")

        def err(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
            payload = {"code": code, "message": message, "details": details or {}}
            payload["details"].setdefault("request_id", rid)
            return payload

        start = end = None
        for field_name in ("starts_at", "ends_at"):
            value = req.get(field_name)
            if not isinstance(value, str):
                return None, err(
                    INVALID_TIMESTAMP,
                    f"字段 {field_name} 必须是字符串时间戳",
                    {"field": field_name, "value": value},
                )
            try:
                parsed = parse_shanghai(value, field=field_name)
            except StructuredError as exc:
                d = dict(exc.details)
                d.setdefault("request_id", rid)
                return None, err(exc.code, exc.message, d)
            if field_name == "starts_at":
                start = parsed
            else:
                end = parsed

        assert start is not None and end is not None
        if end <= start:
            return None, err(
                INVALID_INTERVAL,
                "ends_at 必须严格晚于 starts_at",
                {"starts_at": iso(start), "ends_at": iso(end)},
            )
        if not same_calendar_day(start, end):
            return None, err(
                CROSSES_MIDNIGHT,
                "预约不得跨午夜（起止必须位于 Asia/Shanghai 同一自然日）",
                {"starts_at": iso(start), "ends_at": iso(end)},
            )

        device_id = req["device_id"]
        zone_id = req["zone_id"]
        operator_id = req["operator_id"]
        if device_id not in self.rules.devices:
            return None, err(UNKNOWN_DEVICE, f"未知设备：{device_id}", {"device_id": device_id})
        if zone_id not in self.rules.zones:
            return None, err(UNKNOWN_ZONE, f"未知区域：{zone_id}", {"zone_id": zone_id})
        if operator_id not in self.rules.operators:
            return None, err(UNKNOWN_OPERATOR, f"未知指导员：{operator_id}",
                             {"operator_id": operator_id})

        device = self.rules.devices[device_id]
        if zone_id not in device.allowed_zones:
            return None, err(
                DEVICE_ZONE_INCOMPATIBLE,
                f"设备 {device_id} 不允许在区域 {zone_id} 使用",
                {"device_id": device_id, "zone_id": zone_id,
                 "allowed_zones": list(device.allowed_zones)},
            )
        operator = self.rules.operators[operator_id]
        if device_id not in operator.devices:
            return None, err(
                OPERATOR_NOT_AUTHORIZED,
                f"指导员 {operator_id} 未获得设备 {device_id} 的操作授权",
                {"operator_id": operator_id, "device_id": device_id,
                 "authorized_devices": list(operator.devices)},
            )

        buffer_minutes = max(
            self.rules.turnaround_minutes,
            int(req.get("charging_minutes_after", 0)),
        )
        booking = Booking(
            request_id=rid,
            device_id=device_id,
            zone_id=zone_id,
            operator_id=operator_id,
            starts_at=start,
            ends_at=end,
            priority=int(req["priority"]),
            buffer_minutes=buffer_minutes,
            input_index=int(req["_input_index"]),
        )
        return booking, None

    # ---------- 分配 ----------
    def allocate(self, valid: Sequence[Booking]) -> None:
        ordered = sorted(valid, key=lambda b: (-b.priority, b.starts_at, b.request_id))
        for b in ordered:
            conflicts = self._check_booking(b, b.starts_at)
            if conflicts:
                is_maintenance = any(c["code"] == MAINTENANCE_WINDOW for c in conflicts)
                decision = DECISION_CONFLICT if is_maintenance else DECISION_DEFERRED
                self.results[b.request_id] = RequestResult(
                    request_id=b.request_id,
                    decision=decision,
                    input_index=b.input_index,
                    conflicts=conflicts,
                    booking=b,
                    overall_next=self.next_available(b),
                )
            else:
                self.bookings.append(b)
                self.results[b.request_id] = RequestResult(
                    request_id=b.request_id,
                    decision=DECISION_ALLOCATED,
                    input_index=b.input_index,
                    booking=b,
                )

    def _check_booking(self, b: Booking, start: datetime) -> list[dict[str, Any]]:
        end = start + b.duration
        conflicts: list[dict[str, Any]] = []
        conflicts.append(self._check_singleton(
            "device", b.device_id, DEVICE_OVERLAP,
            self._bookings("device", b.device_id), b, start, end,
            maintenance=self.rules.devices[b.device_id].maintenance,
        ))
        conflicts.append(self._check_singleton(
            "operator", b.operator_id, OPERATOR_OVERLAP,
            self._bookings("operator", b.operator_id), b, start, end,
        ))
        conflicts.append(self._check_zone(b, start, end))

        # 维护窗口：设备在维护（含新预约缓冲侵入）时不可用
        blocking_windows = [
            w for w in self.rules.devices[b.device_id].maintenance
            if _hard_overlap(start, end + timedelta(minutes=b.buffer_minutes),
                             w.starts_at, w.ends_at)
        ]
        if blocking_windows:
            nxt = singleton_next_available(
                start, b.duration, b.buffer_minutes,
                self._bookings("device", b.device_id),
                self.rules.devices[b.device_id].maintenance,
            )
            conflicts.append({
                "resource_type": "device",
                "resource_id": b.device_id,
                "code": MAINTENANCE_WINDOW,
                "blocking_request_ids": [],
                "blocking_maintenance": [
                    {"starts_at": iso(w.starts_at), "ends_at": iso(w.ends_at)}
                    for w in blocking_windows
                ],
                "next_available_at": iso(nxt),
            })
        return [c for c in conflicts if c]

    def _check_singleton(
        self,
        resource_type: str,
        resource_id: str,
        hard_code: str,
        others: list[Booking],
        b: Booking,
        start: datetime,
        end: datetime,
        maintenance: Sequence[MaintenanceWindow] = (),
    ) -> dict[str, Any] | None:
        hard = [x for x in others if _hard_overlap(start, end, x.starts_at, x.ends_at)]
        if hard:
            code, blockers = hard_code, hard
        else:
            # 任一侧缓冲不足：新预约的缓冲撞上后约，或前约缓冲撞上本预约
            buffered_end = end + timedelta(minutes=b.buffer_minutes)
            buffer = [
                x for x in others
                if _hard_overlap(start, buffered_end, x.starts_at, x.block_ends_at)
            ]
            if not buffer:
                return None
            code, blockers = TURNAROUND_REQUIRED, buffer
        nxt = singleton_next_available(
            b.starts_at, b.duration, b.buffer_minutes, others, maintenance
        )
        return {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "code": code,
            "blocking_request_ids": [x.request_id for x in blockers],
            "next_available_at": iso(nxt),
        }

    def _check_zone(self, b: Booking, start: datetime, end: datetime) -> dict[str, Any] | None:
        zone = self.rules.zones[b.zone_id]
        others = self._bookings("zone", b.zone_id)
        hard = [x for x in others if _hard_overlap(start, end, x.starts_at, x.ends_at)]
        buffered_end = end + timedelta(minutes=b.buffer_minutes)
        buffer = [
            x for x in others
            if not _hard_overlap(start, end, x.starts_at, x.ends_at)
            and _hard_overlap(start, buffered_end, x.starts_at, x.block_ends_at)
        ]
        if len(hard) >= zone.capacity:
            code, blockers = ZONE_CAPACITY, hard
        elif len(hard) + len(buffer) >= zone.capacity:
            code, blockers = TURNAROUND_REQUIRED, hard + buffer
        else:
            return None
        nxt = zone_next_available(b.starts_at, b.duration, b.buffer_minutes, others, zone.capacity)
        return {
            "resource_type": "zone",
            "resource_id": b.zone_id,
            "code": code,
            "blocking_request_ids": [x.request_id for x in blockers],
            "next_available_at": iso(nxt),
        }

    # ---------- 查询 ----------
    def next_available(self, b: Booking) -> datetime:
        """建议时段：设备/指导员/区域同时空闲的最早起点（定点迭代）。"""
        t = b.starts_at
        for _ in range(_MAX_NEXT_ITER):
            candidates = [
                singleton_next_available(
                    t, b.duration, b.buffer_minutes,
                    self._bookings("device", b.device_id),
                    self.rules.devices[b.device_id].maintenance,
                ),
                singleton_next_available(
                    t, b.duration, b.buffer_minutes,
                    self._bookings("operator", b.operator_id),
                    (),
                ),
                zone_next_available(
                    t, b.duration, b.buffer_minutes,
                    self._bookings("zone", b.zone_id),
                    self.rules.zones[b.zone_id].capacity,
                ),
            ]
            nxt = max(candidates)
            if nxt == t:
                return t
            t = nxt
        raise RuntimeError("下一可用时段搜索未收敛")  # pragma: no cover

    def timeline(self, resource_type: str, resource_id: str) -> list[dict[str, Any]]:
        if resource_type == "device":
            if resource_id not in self.rules.devices:
                raise StructuredError("RESOURCE_NOT_FOUND", f"未知设备：{resource_id}",
                                      {"resource_type": resource_type, "resource_id": resource_id},
                                      status_code=404)
            items: list[dict[str, Any]] = [
                {
                    "kind": "maintenance",
                    "resource_type": "device",
                    "resource_id": resource_id,
                    "request_id": None,
                    "starts_at": iso(w.starts_at),
                    "ends_at": iso(w.ends_at),
                }
                for w in self.rules.devices[resource_id].maintenance
            ]
        elif resource_type == "zone":
            if resource_id not in self.rules.zones:
                raise StructuredError("RESOURCE_NOT_FOUND", f"未知区域：{resource_id}",
                                      {"resource_type": resource_type, "resource_id": resource_id},
                                      status_code=404)
            items = []
        elif resource_type == "operator":
            if resource_id not in self.rules.operators:
                raise StructuredError("RESOURCE_NOT_FOUND", f"未知指导员：{resource_id}",
                                      {"resource_type": resource_type, "resource_id": resource_id},
                                      status_code=404)
            items = []
        else:
            raise StructuredError(
                "RESOURCE_NOT_FOUND",
                "resource_type 仅支持 device/zone/operator",
                {"resource_type": resource_id},
                status_code=404,
            )
        items.extend(
            {
                "kind": "allocated",
                "resource_type": resource_type,
                "resource_id": resource_id,
                "request_id": x.request_id,
                "starts_at": iso(x.starts_at),
                "ends_at": iso(x.ends_at),
                "buffer_until": iso(x.block_ends_at),
            }
            for x in self._bookings(resource_type, resource_id)
        )
        items.sort(key=lambda it: (it["starts_at"], it.get("request_id") or ""))
        return items


# ---------------- 纯函数 ----------------

def _hard_overlap(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    """半开区间相交（首尾相接不算重叠）。"""
    return s1 < e2 and s2 < e1


def singleton_next_available(
    earliest: datetime,
    duration: timedelta,
    buffer_min: int,
    bookings: Iterable[Booking],
    maintenance: Iterable[MaintenanceWindow] = (),
) -> datetime:
    """设备/指导员类（容量 1）资源的下一可用起始时刻。

    既有预约 x（硬 [xs,xe)，缓冲到 xb=xe+xb_buf）阻塞新预约起始点 t 的区间为
    (xs - d - b_new, xb)：此区间内新预约要么与 x 硬撞，要么某一侧缓冲不足。
    维护窗口 w 阻塞区间为 (ws - d - b_new, we)。
    """
    blocked: list[tuple[datetime, datetime]] = []
    delta = duration + timedelta(minutes=buffer_min)
    for x in bookings:
        blocked.append((x.starts_at - delta, x.block_ends_at))
    for w in maintenance:
        blocked.append((w.starts_at - delta, w.ends_at))

    t = earliest
    for _ in range(_MAX_NEXT_ITER):
        covering = [(lo, hi) for lo, hi in blocked if lo <= t < hi]
        if not covering:
            return t
        t = max(hi for _, hi in covering)
    raise RuntimeError("下一可用时段搜索未收敛")  # pragma: no cover


def zone_next_available(
    earliest: datetime,
    duration: timedelta,
    buffer_min: int,
    bookings: Iterable[Booking],
    capacity: int,
) -> datetime:
    """容量 C 的区域：同一时刻允许 C 个占用；第 C+1 个起始点被阻塞。"""
    delta = duration + timedelta(minutes=buffer_min)
    blocked = [(x.starts_at - delta, x.block_ends_at) for x in bookings]
    t = earliest
    for _ in range(_MAX_NEXT_ITER):
        active = [(lo, hi) for lo, hi in blocked if lo <= t < hi]
        if len(active) < capacity:
            return t
        t = min(hi for _, hi in active)
    raise RuntimeError("区域下一可用时段搜索未收敛")  # pragma: no cover
