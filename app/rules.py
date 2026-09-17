"""规则加载：规则只能从 fixtures 读取。

加载时把维护窗口解析为 Asia/Shanghai 时间；对外视图必须脱敏 internal_notes。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .timeutil import SHANGHAI, parse_shanghai

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = ROOT / "fixtures" / "rules.json"

# 永远不允许出现在任何响应中的字段
_SECRET_KEYS = ("internal_notes", "notes", "remark", "remarks")


@dataclass(frozen=True)
class MaintenanceWindow:
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class Device:
    device_id: str
    allowed_zones: tuple[str, ...]
    maintenance: tuple[MaintenanceWindow, ...]


@dataclass(frozen=True)
class Zone:
    zone_id: str
    capacity: int


@dataclass(frozen=True)
class Operator:
    operator_id: str
    devices: tuple[str, ...]


@dataclass(frozen=True)
class Rules:
    version: str
    timezone: str
    turnaround_minutes: int
    allocation_order: tuple[str, ...]
    devices: dict[str, Device]
    zones: dict[str, Zone]
    operators: dict[str, Operator]
    conflict_codes: tuple[str, ...]
    raw: dict = field(repr=False, default_factory=dict)

    def public_view(self) -> dict:
        """脱敏后的规则视图（可直接作为 /rules 响应）。"""
        return {
            "version": self.version,
            "timezone": self.timezone,
            "turnaround_minutes": self.turnaround_minutes,
            "allocation_order": list(self.allocation_order),
            "devices": [
                {
                    "device_id": d.device_id,
                    "allowed_zones": list(d.allowed_zones),
                    "maintenance": [
                        {
                            "starts_at": w.starts_at.isoformat(),
                            "ends_at": w.ends_at.isoformat(),
                        }
                        for w in d.maintenance
                    ],
                }
                for d in self.devices.values()
            ],
            "zones": [
                {"zone_id": z.zone_id, "capacity": z.capacity}
                for z in self.zones.values()
            ],
            "operators": [
                {"operator_id": o.operator_id, "devices": list(o.devices)}
                for o in self.operators.values()
            ],
            "conflict_codes": list(self.conflict_codes),
        }


def _parse_window(item: dict, device_id: str) -> MaintenanceWindow:
    start = parse_shanghai(item.get("starts_at"), field="maintenance.starts_at")
    end = parse_shanghai(item.get("ends_at"), field="maintenance.ends_at")
    if end <= start:
        raise ValueError(f"设备 {device_id} 存在非法维护区间")
    return MaintenanceWindow(start, end)


def load_rules(path: str | Path | None = None) -> Rules:
    path = Path(path) if path else DEFAULT_RULES_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("timezone") != "Asia/Shanghai":
        raise ValueError("rules.json 的 timezone 必须为 Asia/Shanghai")
    turnaround = int(data["turnaround_minutes"])
    if turnaround < 0:
        raise ValueError("turnaround_minutes 不能为负")

    devices: dict[str, Device] = {}
    for item in data["devices"]:
        dev_id = item["device_id"]
        devices[dev_id] = Device(
            device_id=dev_id,
            allowed_zones=tuple(item["allowed_zones"]),
            maintenance=tuple(_parse_window(w, dev_id) for w in item.get("maintenance", [])),
        )
    zones = {
        item["zone_id"]: Zone(item["zone_id"], int(item["capacity"]))
        for item in data["zones"]
    }
    operators = {
        item["operator_id"]: Operator(item["operator_id"], tuple(item["devices"]))
        for item in data["operators"]
    }
    return Rules(
        version=str(data.get("version", "")),
        timezone=data["timezone"],
        turnaround_minutes=turnaround,
        allocation_order=tuple(data.get("allocation_order", [])),
        devices=devices,
        zones=zones,
        operators=operators,
        conflict_codes=tuple(data.get("conflict_codes", [])),
        raw=data,
    )


class RulesLoader:
    """进程内缓存的规则加载器（规则来源唯一：fixtures）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = path
        self._rules: Rules | None = None
        self._lock = threading.Lock()

    def get(self) -> Rules:
        if self._rules is None:
            with self._lock:
                if self._rules is None:
                    self._rules = load_rules(self._path)
        return self._rules


def strip_secrets(obj):
    """递归移除内部备注字段（额外保险，正常路径不会携带）。"""
    if isinstance(obj, dict):
        return {k: strip_secrets(v) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, list):
        return [strip_secrets(v) for v in obj]
    return obj
