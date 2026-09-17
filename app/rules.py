"""Loading and validated access to the lab rules fixture.

Rules come *only* from ``fixtures/rules.json`` (path overridable through the
``RULES_PATH`` environment variable for tests); there is no database, no
campus-system connection and no other source of truth.

``internal_notes`` and any other field not part of the public rule view are
stripped by :meth:`Rules.public_view` so they can never leak through an API.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import timeutil

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "rules.json"

# Fields the public rules view is allowed to expose. Everything else in the
# fixture (notably ``internal_notes``) is intentionally absent.
_PUBLIC_TOP_LEVEL = (
    "version",
    "timezone",
    "turnaround_minutes",
    "allocation_order",
    "devices",
    "zones",
    "operators",
    "conflict_codes",
)


class RulesLoadError(RuntimeError):
    """Raised only when the service fixture itself is broken (HTTP 500)."""


@dataclass(frozen=True)
class MaintenanceWindow:
    starts_at: datetime
    ends_at: datetime

    def as_json(self) -> dict:
        return {
            "starts_at": timeutil.to_iso(self.starts_at),
            "ends_at": timeutil.to_iso(self.ends_at),
        }


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
    devices: dict[str, Device] = field(default_factory=dict)
    zones: dict[str, Zone] = field(default_factory=dict)
    operators: dict[str, Operator] = field(default_factory=dict)
    conflict_codes: tuple[str, ...] = ()
    source_path: str = ""

    # -- lookup helpers ---------------------------------------------------
    def device(self, device_id: str) -> Device | None:
        return self.devices.get(device_id)

    def zone(self, zone_id: str) -> Zone | None:
        return self.zones.get(zone_id)

    def operator(self, operator_id: str) -> Operator | None:
        return self.operators.get(operator_id)

    # -- serialisation ----------------------------------------------------
    def public_view(self) -> dict:
        """JSON-safe rules document guaranteed never to contain internal data."""
        return {
            "version": self.version,
            "timezone": self.timezone,
            "turnaround_minutes": self.turnaround_minutes,
            "allocation_order": list(self.allocation_order),
            "devices": [
                {
                    "device_id": d.device_id,
                    "allowed_zones": list(d.allowed_zones),
                    "maintenance": [w.as_json() for w in d.maintenance],
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


def _parse_window(raw: dict, *, where: str) -> MaintenanceWindow:
    start = timeutil.parse_timestamp(raw.get("starts_at"), field="starts_at")
    end = timeutil.parse_timestamp(raw.get("ends_at"), field="ends_at")
    if isinstance(start, timeutil.TimeParseError) or isinstance(
        end, timeutil.TimeParseError
    ):
        raise RulesLoadError(f"invalid maintenance timestamp in {where}")
    if end <= start:
        raise RulesLoadError(f"maintenance window must end after start in {where}")
    return MaintenanceWindow(start, end)


def load_rules(path: str | os.PathLike[str] | None = None) -> Rules:
    """Read and structurally validate the rules fixture.

    Fixture problems (missing file, bad shape) are a server-side failure,
    never a client error, so they surface as :class:`RulesLoadError`.
    """
    rules_path = Path(path) if path else DEFAULT_RULES_PATH
    try:
        raw = json.loads(rules_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RulesLoadError(f"rules fixture not found: {rules_path}") from exc
    except json.JSONDecodeError as exc:
        raise RulesLoadError(f"rules fixture is not valid JSON: {exc}") from exc

    required = (
        "version",
        "timezone",
        "turnaround_minutes",
        "allocation_order",
        "devices",
        "zones",
        "operators",
        "conflict_codes",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise RulesLoadError(f"rules fixture missing keys: {', '.join(missing)}")
    if raw["timezone"] != timeutil.LAB_TIMEZONE:
        raise RulesLoadError(
            f"rules timezone must be {timeutil.LAB_TIMEZONE}, got {raw['timezone']!r}"
        )

    devices: dict[str, Device] = {}
    for item in raw["devices"]:
        device_id = item["device_id"]
        if device_id in devices:
            raise RulesLoadError(f"duplicate device_id in fixture: {device_id}")
        windows = tuple(
            _parse_window(w, where=f"device {device_id}")
            for w in item.get("maintenance", [])
        )
        devices[device_id] = Device(
            device_id=device_id,
            allowed_zones=tuple(item["allowed_zones"]),
            maintenance=windows,
        )

    zones: dict[str, Zone] = {}
    for item in raw["zones"]:
        zone_id = item["zone_id"]
        if zone_id in zones:
            raise RulesLoadError(f"duplicate zone_id in fixture: {zone_id}")
        capacity = item["capacity"]
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise RulesLoadError(f"zone {zone_id} has invalid capacity: {capacity!r}")
        zones[zone_id] = Zone(zone_id=zone_id, capacity=capacity)

    operators: dict[str, Operator] = {}
    for item in raw["operators"]:
        operator_id = item["operator_id"]
        if operator_id in operators:
            raise RulesLoadError(f"duplicate operator_id in fixture: {operator_id}")
        operators[operator_id] = Operator(
            operator_id=operator_id, devices=tuple(item["devices"])
        )

    unknown_zone = next(
        (
            (d.device_id, z)
            for d in devices.values()
            for z in d.allowed_zones
            if z not in zones
        ),
        None,
    )
    if unknown_zone:
        raise RulesLoadError(
            f"device {unknown_zone[0]} references unknown zone {unknown_zone[1]}"
        )
    unknown_device = next(
        (
            (o.operator_id, d)
            for o in operators.values()
            for d in o.devices
            if d not in devices
        ),
        None,
    )
    if unknown_device:
        raise RulesLoadError(
            f"operator {unknown_device[0]} references unknown device {unknown_device[1]}"
        )

    return Rules(
        version=str(raw["version"]),
        timezone=raw["timezone"],
        turnaround_minutes=int(raw["turnaround_minutes"]),
        allocation_order=tuple(raw["allocation_order"]),
        devices=devices,
        zones=zones,
        operators=operators,
        conflict_codes=tuple(raw["conflict_codes"]),
        source_path=str(rules_path),
    )


def load_default_rules() -> Rules:
    """Convenience loader honouring the ``RULES_PATH`` override."""
    return load_rules(os.environ.get("RULES_PATH"))
