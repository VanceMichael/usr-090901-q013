"""Shared pytest fixtures and tiny builders for schedule drafts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.rules import load_rules

ROOT = Path(__file__).resolve().parent.parent
REAL_RULES = ROOT / "fixtures" / "rules.json"


@pytest.fixture
def rules():
    return load_rules(REAL_RULES)


@pytest.fixture
def extended_rules(tmp_path):
    """Rules with a third device/operator so zone capacity 2 can be saturated."""
    raw = json.loads(REAL_RULES.read_text(encoding="utf-8"))
    raw["devices"].append(
        {"device_id": "ROVER-03", "allowed_zones": ["ZONE-D"], "maintenance": []}
    )
    raw["operators"].append({"operator_id": "OP-303", "devices": ["ROVER-03"]})
    path = tmp_path / "rules-extended.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_rules(path)


def draft(
    request_id,
    *,
    device="ARM-07",
    zone="ZONE-C",
    operator="OP-101",
    start="2026-09-09T09:00:00+08:00",
    end="2026-09-09T10:00:00+08:00",
    priority=3,
    charging=None,
):
    """Build a request dict matching contracts/request.schema.json."""
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


def envelope(requests, schedule_id="lab-schedule-test01"):
    return {
        "schedule_id": schedule_id,
        "timezone": "Asia/Shanghai",
        "requests": requests,
    }


def allocate(rules, requests, schedule_id="lab-schedule-test01"):
    """Validate + run a batch, returning the ordered decision list."""
    from app.engine import run_schedule
    from app.validator import validate_batch

    validation, err = validate_batch(envelope(requests, schedule_id), rules)
    assert err is None, err
    results, _engine = run_schedule(rules, validation.valid, validation.draft_errors)
    return results, validation
