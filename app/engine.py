"""Deterministic resource allocation engine.

Drafts are processed in the fixture-defined order
``priority desc, starts_at asc, request_id asc`` and greedily take every
resource they need. A draft never partially allocates: on the first blocked
resource (fixed inspection order: maintenance -> device -> zone -> operator)
it receives one decision carrying the concrete resource, the blocking
bookings and the next instant the resource is free.

Decisions
---------
* ``allocated``  - device, a zone slot and the operator are all secured.
* ``conflict``   - a hard clash at the requested instant: maintenance
                   (``MAINTENANCE_WINDOW``) or simultaneous use of a device,
                   full zone or operator (``DEVICE_OVERLAP`` /
                   ``ZONE_CAPACITY`` / ``OPERATOR_OVERLAP``).
* ``deferred``   - activities do not overlap, but the required
                   preparation/charging gap (``TURNAROUND_REQUIRED``) cannot
                   be kept; retrying at ``next_available_at`` honours it.

Structurally invalid drafts never reach the engine; the API reports them as
separate structured ``rejected`` items so valid drafts are unaffected.

Time model
----------
* Activity occupies the half-open interval ``[start, end)``.
* A device/operator is additionally blocked through the tail
  ``[end, end + max(turnaround_minutes, charging_minutes_after))``.
* Zone slots are occupied for the activity only; the device tail keeps the
  next group out of the physical area through the device/operator constraint.
* Maintenance blocks the device for its whole span and also rejects tails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import errors, timeutil
from .rules import Rules, Zone
from .validator import DraftError, ValidDraft

RESOURCE_DEVICE = "device"
RESOURCE_ZONE = "zone"
RESOURCE_OPERATOR = "operator"

# Fixed inspection order for conflicts (deterministic code selection).
_CODE_PRIORITY = (
    errors.MAINTENANCE_WINDOW,
    errors.DEVICE_OVERLAP,
    errors.ZONE_CAPACITY,
    errors.OPERATOR_OVERLAP,
    errors.TURNAROUND_REQUIRED,
)
_DEFERRED_CODES = frozenset({errors.TURNAROUND_REQUIRED})


@dataclass(frozen=True)
class Block:
    """One earlier commitment that prevents a draft from taking a resource."""

    request_id: str
    resource_type: str
    resource_id: str
    blocked_from: datetime
    blocked_until: datetime
    reason: str

    def as_json(self) -> dict:
        return {
            "request_id": self.request_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "blocked_from": timeutil.to_iso(self.blocked_from),
            "blocked_until": timeutil.to_iso(self.blocked_until),
            "reason": self.reason,
        }


@dataclass
class Booking:
    draft: ValidDraft
    tail_end: datetime

    def as_allocation(self) -> dict:
        d = self.draft
        return {
            "request_id": d.request_id,
            "starts_at": timeutil.to_iso(d.starts_at),
            "ends_at": timeutil.to_iso(d.ends_at),
            "device_id": d.device_id,
            "zone_id": d.zone_id,
            "operator_id": d.operator_id,
            "priority": d.priority,
            "charging_minutes_after": d.charging_minutes_after,
            "ready_at": timeutil.to_iso(self.tail_end),
        }


def _activity_overlap(a_start, a_end, b_start, b_end) -> bool:
    return timeutil.Interval(a_start, a_end).overlaps(
        timeutil.Interval(b_start, b_end)
    )


@dataclass
class SerialResource:
    """A single-user resource (one device or one operator)."""

    resource_type: str
    resource_id: str
    bookings: list[Booking] = field(default_factory=list)

    def check(
        self, draft: ValidDraft, tail_end: datetime, overlap_code: str
    ) -> list[Block]:
        """Return hard-overlap blocks, then turnaround-gap blocks.

        ``tail_end`` is ``ends_at + max(turnaround, charging)`` for ``draft``
        and ``held.tail_end`` plays the same role for a committed booking.
        Higher-priority drafts are processed first, so a held booking can
        start *later* than the draft; its tail then caps how late the draft
        may run.
        """
        overlap_blocks: list[Block] = []
        turnaround_blocks: list[Block] = []
        for held in self.bookings:
            h = held.draft
            if _activity_overlap(
                draft.starts_at, draft.ends_at, h.starts_at, h.ends_at
            ):
                overlap_blocks.append(
                    Block(
                        request_id=h.request_id,
                        resource_type=self.resource_type,
                        resource_id=self.resource_id,
                        blocked_from=h.starts_at,
                        blocked_until=held.tail_end,
                        reason=overlap_code,
                    )
                )
                continue
            # Activities are disjoint from here on; enforce the tail gap in
            # both temporal directions.
            if draft.ends_at <= h.starts_at and tail_end > h.starts_at:
                # Draft runs first; its preparation/charging tail runs into
                # the later held booking.
                turnaround_blocks.append(
                    Block(
                        request_id=h.request_id,
                        resource_type=self.resource_type,
                        resource_id=self.resource_id,
                        blocked_from=h.starts_at,
                        blocked_until=held.tail_end,
                        reason=errors.TURNAROUND_REQUIRED,
                    )
                )
            elif h.ends_at <= draft.starts_at and draft.starts_at < held.tail_end:
                # Draft starts before the held booking's tail has elapsed.
                turnaround_blocks.append(
                    Block(
                        request_id=h.request_id,
                        resource_type=self.resource_type,
                        resource_id=self.resource_id,
                        blocked_from=h.ends_at,
                        blocked_until=held.tail_end,
                        reason=errors.TURNAROUND_REQUIRED,
                    )
                )
        return overlap_blocks + turnaround_blocks

    def commit(self, booking: Booking) -> None:
        self.bookings.append(booking)


@dataclass
class ZoneState:
    """Capacity-tracked zone: each slot holds non-overlapping activities."""

    zone: Zone
    slots: list[list[Booking]]

    @classmethod
    def for_zone(cls, zone: Zone) -> "ZoneState":
        return cls(zone=zone, slots=[[] for _ in range(zone.capacity)])

    def _free_slot(self, draft: ValidDraft) -> int | None:
        for index, slot in enumerate(self.slots):
            if all(
                not _activity_overlap(
                    draft.starts_at,
                    draft.ends_at,
                    held.draft.starts_at,
                    held.draft.ends_at,
                )
                for held in slot
            ):
                return index
        return None

    def check(self, draft: ValidDraft) -> list[Block]:
        """ZONE_CAPACITY blocks only once *every* slot is simultaneously busy."""
        if self._free_slot(draft) is not None:
            return []
        blocks: list[Block] = []
        for slot in self.slots:
            overlapping = [
                held
                for held in slot
                if _activity_overlap(
                    draft.starts_at,
                    draft.ends_at,
                    held.draft.starts_at,
                    held.draft.ends_at,
                )
            ]
            # The slot frees only when its latest overlapping activity ends;
            # report that booking as the slot's representative blocker.
            latest = max(overlapping, key=lambda b: b.draft.ends_at)
            blocks.append(
                Block(
                    request_id=latest.draft.request_id,
                    resource_type=RESOURCE_ZONE,
                    resource_id=self.zone.zone_id,
                    blocked_from=latest.draft.starts_at,
                    blocked_until=latest.draft.ends_at,
                    reason=errors.ZONE_CAPACITY,
                )
            )
        return blocks

    def commit(self, draft: ValidDraft, booking: Booking) -> None:
        index = self._free_slot(draft)
        if index is None:
            raise RuntimeError(f"zone {self.zone.zone_id} committed without a slot")
        self.slots[index].append(booking)


class ScheduleEngine:
    def __init__(self, rules: Rules):
        self.rules = rules
        self.devices = {
            device_id: SerialResource(RESOURCE_DEVICE, device_id)
            for device_id in rules.devices
        }
        self.operators = {
            operator_id: SerialResource(RESOURCE_OPERATOR, operator_id)
            for operator_id in rules.operators
        }
        self.zones = {
            zone_id: ZoneState.for_zone(zone)
            for zone_id, zone in rules.zones.items()
        }
        self.allocations: list[Booking] = []

    def tail_end(self, draft: ValidDraft) -> datetime:
        minutes = max(self.rules.turnaround_minutes, draft.charging_minutes_after)
        return timeutil.add_minutes(draft.ends_at, minutes)

    def inspect(self, draft: ValidDraft) -> dict:
        """Collect *every* blocking condition without committing the draft.

        Used by the single-draft explanation endpoint: unlike
        :meth:`evaluate` it reports all resource checks at once.
        """
        tail_end = self.tail_end(draft)
        device = self.rules.device(draft.device_id)
        grouped: dict[str, list[Block]] = {code: [] for code in _CODE_PRIORITY}

        for window in device.maintenance:
            if timeutil.Interval(draft.starts_at, tail_end).overlaps(
                timeutil.Interval(window.starts_at, window.ends_at)
            ):
                grouped[errors.MAINTENANCE_WINDOW].append(
                    Block(
                        request_id="MAINTENANCE",
                        resource_type=RESOURCE_DEVICE,
                        resource_id=device.device_id,
                        blocked_from=window.starts_at,
                        blocked_until=window.ends_at,
                        reason=errors.MAINTENANCE_WINDOW,
                    )
                )

        for block in self.devices[device.device_id].check(
            draft, tail_end, errors.DEVICE_OVERLAP
        ):
            grouped[block.reason].append(block)
        for block in self.zones[draft.zone_id].check(draft):
            grouped[block.reason].append(block)
        for block in self.operators[draft.operator_id].check(
            draft, tail_end, errors.OPERATOR_OVERLAP
        ):
            grouped[block.reason].append(block)
        return {"tail_end": tail_end, "blocks": grouped}

    def evaluate(self, draft: ValidDraft) -> dict:
        tail_end = self.tail_end(draft)
        device = self.rules.device(draft.device_id)

        grouped: dict[str, list[Block]] = {code: [] for code in _CODE_PRIORITY}

        # Maintenance: the activity *and* its tail must clear every window.
        for window in device.maintenance:
            if timeutil.Interval(draft.starts_at, tail_end).overlaps(
                timeutil.Interval(window.starts_at, window.ends_at)
            ):
                grouped[errors.MAINTENANCE_WINDOW].append(
                    Block(
                        request_id="MAINTENANCE",
                        resource_type=RESOURCE_DEVICE,
                        resource_id=device.device_id,
                        blocked_from=window.starts_at,
                        blocked_until=window.ends_at,
                        reason=errors.MAINTENANCE_WINDOW,
                    )
                )

        for block in self.devices[device.device_id].check(
            draft, tail_end, errors.DEVICE_OVERLAP
        ):
            grouped[block.reason].append(block)

        for block in self.zones[draft.zone_id].check(draft):
            grouped[block.reason].append(block)

        for block in self.operators[draft.operator_id].check(
            draft, tail_end, errors.OPERATOR_OVERLAP
        ):
            grouped[block.reason].append(block)

        for code in _CODE_PRIORITY:
            if grouped[code]:
                return self._blocked_record(draft, code, grouped[code])

        booking = Booking(draft=draft, tail_end=tail_end)
        self.devices[device.device_id].commit(booking)
        self.zones[draft.zone_id].commit(draft, booking)
        self.operators[draft.operator_id].commit(booking)
        self.allocations.append(booking)
        return {
            "decision": "allocated",
            "request_id": draft.request_id,
            "index": draft.index,
            "allocation": booking.as_allocation(),
        }

    def _blocked_record(
        self, draft: ValidDraft, code: str, blocks: list[Block]
    ) -> dict:
        decision = "deferred" if code in _DEFERRED_CODES else "conflict"
        conflicting_resources = sorted(
            {f"{b.resource_type}:{b.resource_id}" for b in blocks}
        )
        blocking_request_ids = sorted(
            {b.request_id for b in blocks if b.request_id != "MAINTENANCE"}
        )
        # Earliest release of any involved commitment at/after the requested
        # start - the first instant at which retrying makes sense.
        later_releases = [
            b.blocked_until for b in blocks if b.blocked_until > draft.starts_at
        ]
        next_available = min(later_releases or [b.blocked_until for b in blocks])
        return {
            "decision": decision,
            "request_id": draft.request_id,
            "index": draft.index,
            "conflict_code": code,
            "conflicting_resources": conflicting_resources,
            "blocking_request_ids": blocking_request_ids,
            "blocked_by": [b.as_json() for b in blocks],
            "next_available_at": timeutil.to_iso(next_available),
        }


def allocation_key(draft: ValidDraft) -> tuple:
    """priority desc, starts_at asc, request_id asc (from the fixture)."""
    return (-draft.priority, draft.starts_at, draft.request_id)


def run_schedule(
    rules: Rules, drafts: list[ValidDraft], draft_errors: list[DraftError]
) -> tuple[list[dict], ScheduleEngine]:
    """Allocate valid drafts in priority order; pair decisions to input order.

    Returns the index-ordered decision list and the populated engine (used
    for timeline snapshots).
    """
    engine = ScheduleEngine(rules)
    decisions: dict[int, dict] = {}

    for draft in sorted(drafts, key=allocation_key):
        decisions[draft.index] = engine.evaluate(draft)

    for err in draft_errors:
        decisions[err.index] = {
            "decision": "rejected",
            "request_id": err.request_id,
            "index": err.index,
            "error": err.as_json(),
        }

    return [decisions[i] for i in sorted(decisions)], engine


def timeline_snapshot(engine: ScheduleEngine) -> dict[str, list[dict]]:
    """Public per-resource timeline built only from committed bookings."""
    entries: dict[str, list[dict]] = {}

    def add(resource_type: str, resource_id: str, entry: dict) -> None:
        entries.setdefault(f"{resource_type}:{resource_id}", []).append(entry)

    for booking in engine.allocations:
        d = booking.draft
        add(
            RESOURCE_DEVICE,
            d.device_id,
            {
                "kind": "booking",
                "request_id": d.request_id,
                "starts_at": timeutil.to_iso(d.starts_at),
                "ends_at": timeutil.to_iso(booking.tail_end),
                "activity_ends_at": timeutil.to_iso(d.ends_at),
                "detail": "activity plus preparation/charging tail",
            },
        )
        add(
            RESOURCE_OPERATOR,
            d.operator_id,
            {
                "kind": "booking",
                "request_id": d.request_id,
                "starts_at": timeutil.to_iso(d.starts_at),
                "ends_at": timeutil.to_iso(booking.tail_end),
                "activity_ends_at": timeutil.to_iso(d.ends_at),
                "detail": "activity plus preparation/charging tail",
            },
        )
        add(
            RESOURCE_ZONE,
            d.zone_id,
            {
                "kind": "booking",
                "request_id": d.request_id,
                "starts_at": timeutil.to_iso(d.starts_at),
                "ends_at": timeutil.to_iso(d.ends_at),
                "activity_ends_at": timeutil.to_iso(d.ends_at),
                "detail": "zone activity occupancy",
            },
        )

    for device_id, device in engine.rules.devices.items():
        for window in device.maintenance:
            add(
                RESOURCE_DEVICE,
                device_id,
                {
                    "kind": "maintenance",
                    "request_id": "MAINTENANCE",
                    "starts_at": timeutil.to_iso(window.starts_at),
                    "ends_at": timeutil.to_iso(window.ends_at),
                    "activity_ends_at": timeutil.to_iso(window.ends_at),
                    "detail": "maintenance window",
                },
            )

    for items in entries.values():
        items.sort(key=lambda e: (e["starts_at"], e["request_id"]))
    return entries
