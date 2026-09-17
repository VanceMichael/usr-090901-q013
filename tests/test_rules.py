"""Rules loading: only-fixture sourcing and internal_notes non-leakage."""

import json
from pathlib import Path

import pytest

from app.rules import RulesLoadError, load_rules

ROOT = Path(__file__).resolve().parent.parent
REAL_RULES = ROOT / "fixtures" / "rules.json"


def test_public_view_has_no_internal_notes(rules):
    view = rules.public_view()
    assert "internal_notes" not in view
    dumped = json.dumps(view)
    assert "internal_notes" not in dumped
    assert "Allocation test fixture" not in dumped


def test_public_view_shape(rules):
    view = rules.public_view()
    assert set(view) == {
        "version",
        "timezone",
        "zones",
        "devices",
        "operators",
        "conflict_codes",
        "turnaround_minutes",
        "allocation_order",
    }
    assert view["timezone"] == "Asia/Shanghai"
    assert {z["zone_id"] for z in view["zones"]} == {"ZONE-C", "ZONE-D"}


def test_rules_only_come_from_fixture(rules):
    # The loaded rule set is exactly the fixture content; nothing is invented.
    assert set(rules.devices) == {"ARM-07", "ROVER-02"}
    assert rules.turnaround_minutes == 15
    assert tuple(rules.allocation_order) == (
        "priority_desc",
        "starts_at_asc",
        "request_id_asc",
    )


def test_missing_fixture_is_load_error(tmp_path):
    with pytest.raises(RulesLoadError):
        load_rules(tmp_path / "nope.json")


def test_broken_fixture_is_load_error(tmp_path):
    bad = tmp_path / "rules.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RulesLoadError):
        load_rules(bad)


def test_fixture_with_wrong_timezone_is_load_error(tmp_path):
    raw = json.loads(REAL_RULES.read_text(encoding="utf-8"))
    raw["timezone"] = "UTC"
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RulesLoadError):
        load_rules(path)
