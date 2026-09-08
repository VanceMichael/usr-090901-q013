import json
from pathlib import Path

root = Path("/workspace")
contract = json.loads((root / "contracts/request.schema.json").read_text())
samples = json.loads((root / "fixtures/sample-requests.json").read_text())
rules = json.loads((root / "fixtures/rules.json").read_text())
assert contract["type"] == "object" and contract["additionalProperties"] is False
assert set(contract["required"]) == {"schedule_id", "timezone", "requests"}
assert isinstance(samples, list) and samples
assert rules["timezone"] == "Asia/Shanghai"
device_ids = {item["device_id"] for item in rules["devices"]}
zone_ids = {item["zone_id"] for item in rules["zones"]}
operator_ids = {item["operator_id"] for item in rules["operators"]}
for sample in samples:
    assert sample["timezone"] == rules["timezone"]
    for request in sample["requests"]:
        assert request["device_id"] in device_ids
        assert request["zone_id"] in zone_ids
        assert request["operator_id"] in operator_ids
print(f"scaffold inputs valid: {len(samples)} schedules, {len(device_ids)} devices")
