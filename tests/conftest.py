"""pytest 公共夹具。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.rules import RulesLoader

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def rules_loader() -> RulesLoader:
    return RulesLoader(ROOT / "fixtures" / "rules.json")


@pytest.fixture
def custom_rules_loader(tmp_path):
    """构造可定制规则文件的加载器（规则仍来自 fixtures 风格文件）。"""

    default = json.loads((ROOT / "fixtures" / "rules.json").read_text(encoding="utf-8"))

    def _make(**overrides) -> RulesLoader:
        data = json.loads(json.dumps(default))
        data.update(overrides)
        path = tmp_path / "rules.custom.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return RulesLoader(path)

    return _make


def make_req(
    request_id: str,
    *,
    device_id: str = "ARM-07",
    zone_id: str = "ZONE-C",
    operator_id: str = "OP-101",
    starts_at: str = "2026-09-09T09:00:00+08:00",
    ends_at: str = "2026-09-09T10:00:00+08:00",
    priority: int = 3,
    charging_minutes_after=None,
) -> dict:
    req = {
        "request_id": request_id,
        "device_id": device_id,
        "zone_id": zone_id,
        "operator_id": operator_id,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "priority": priority,
    }
    if charging_minutes_after is not None:
        req["charging_minutes_after"] = charging_minutes_after
    return req


def make_schedule(requests, schedule_id: str = "test-schedule-0001") -> dict:
    return {
        "schedule_id": schedule_id,
        "timezone": "Asia/Shanghai",
        "requests": requests,
    }


@pytest.fixture
def helpers():
    return type("H", (), {
        "make_req": staticmethod(make_req),
        "make_schedule": staticmethod(make_schedule),
    })
