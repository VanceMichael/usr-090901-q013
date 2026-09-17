"""The shipped fixtures must satisfy the JSON Schema contract."""

import json
from pathlib import Path

import jsonschema
import pytest

from app.rules import load_rules

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def schema():
    return json.loads((ROOT / "contracts" / "request.schema.json").read_text())


@pytest.fixture(scope="module")
def samples():
    return json.loads((ROOT / "fixtures" / "sample-requests.json").read_text())


def test_samples_validate(schema, samples):
    for sample in samples:
        jsonschema.validate(sample, schema)


def test_all_sample_requests_engine_compatible(samples):
    rules = load_rules(ROOT / "fixtures" / "rules.json")
    for sample in samples:
        for req in sample["requests"]:
            assert req["device_id"] in rules.devices
            assert req["zone_id"] in rules.zones
            assert req["operator_id"] in rules.operators


def test_schema_rejects_wrong_timezone(schema):
    bad = {
        "schedule_id": "lab-schedule-bad0001",
        "timezone": "UTC",
        "requests": [],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
