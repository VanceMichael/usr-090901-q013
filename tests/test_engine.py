"""Allocation engine: the six scenarios the coordinator cares about.

1. Zone capacity (multiple groups at once, saturation rejected)
2. Maintenance preemption (and tail-into-maintenance)
3. Preparation/charging turnaround gaps (deferred)
4. Priority tie-break (priority desc, starts_at asc, request_id asc)
5. Cross-midnight drafts (rejected upstream; next-day boundary scheduling)
6. Mixed batches (bad drafts isolated, good ones still allocate)
"""

from app import errors

from .conftest import allocate, draft


def by_id(results):
    return {r["request_id"]: r for r in results}


# ---------------------------------------------------------------- capacity
def test_zone_capacity_allows_two_simultaneous_groups(extended_rules):
    requests = [
        draft("REQ-ROVER-401", device="ROVER-02", zone="ZONE-D",
              operator="OP-202", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T09:45:00+08:00"),
        draft("REQ-ROVER-402", device="ROVER-03", zone="ZONE-D",
              operator="OP-303", start="2026-09-09T09:05:00+08:00",
              end="2026-09-09T09:50:00+08:00"),
    ]
    results, _ = allocate(extended_rules, requests)
    assert all(r["decision"] == "allocated" for r in results)


def test_zone_capacity_saturation_is_conflict(extended_rules):
    requests = [
        draft("REQ-ROVER-411", device="ROVER-02", zone="ZONE-D",
              operator="OP-202", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T10:00:00+08:00"),
        draft("REQ-ROVER-412", device="ROVER-03", zone="ZONE-D",
              operator="OP-303", start="2026-09-09T09:10:00+08:00",
              end="2026-09-09T09:40:00+08:00"),
        # ARM-07 may also use ZONE-D; with both slots busy this must fail.
        draft("REQ-ARM-413", device="ARM-07", zone="ZONE-D",
              operator="OP-202", start="2026-09-09T09:20:00+08:00",
              end="2026-09-09T09:45:00+08:00"),
    ]
    results, _ = allocate(extended_rules, requests)
    decisions = by_id(results)
    assert decisions["REQ-ROVER-411"]["decision"] == "allocated"
    assert decisions["REQ-ROVER-412"]["decision"] == "allocated"
    third = decisions["REQ-ARM-413"]
    assert third["decision"] == "conflict"
    assert third["conflict_code"] == errors.ZONE_CAPACITY
    assert third["conflicting_resources"] == ["zone:ZONE-D"]
    assert set(third["blocking_request_ids"]) == {"REQ-ROVER-411", "REQ-ROVER-412"}
    # Both occupants end by 10:00; earliest slot frees at 09:40 (REQ-ROVER-412).
    assert third["next_available_at"] == "2026-09-09T09:40:00+08:00"


def test_third_group_fits_after_one_activity_ends(extended_rules):
    requests = [
        draft("REQ-ROVER-421", device="ROVER-02", zone="ZONE-D",
              operator="OP-202", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T09:30:00+08:00"),
        draft("REQ-ROVER-422", device="ROVER-03", zone="ZONE-D",
              operator="OP-303", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T10:00:00+08:00"),
        draft("REQ-ARM-423", device="ARM-07", zone="ZONE-D",
              operator="OP-101", start="2026-09-09T09:30:00+08:00",
              end="2026-09-09T09:55:00+08:00"),
    ]
    results, _ = allocate(extended_rules, requests)
    decisions = by_id(results)
    assert decisions["REQ-ARM-423"]["decision"] == "allocated"


# ------------------------------------------------------------ maintenance
def test_maintenance_overlap_is_conflict(rules):
    requests = [
        draft("REQ-ARM-501", start="2026-09-09T11:30:00+08:00",
              end="2026-09-09T12:30:00+08:00"),
    ]
    decisions = by_id(allocate(rules, requests)[0])
    rec = decisions["REQ-ARM-501"]
    assert rec["decision"] == "conflict"
    assert rec["conflict_code"] == errors.MAINTENANCE_WINDOW
    assert rec["conflicting_resources"] == ["device:ARM-07"]
    assert rec["blocked_by"][0]["request_id"] == "MAINTENANCE"
    assert rec["next_available_at"] == "2026-09-09T13:00:00+08:00"


def test_booking_plus_tail_ending_exactly_when_maintenance_starts(rules):
    # 11:45 activity end + 15 minute turnaround tail == 12:00 maintenance
    # start: half-open intervals just touch, so it must be allowed.
    requests = [
        draft("REQ-ARM-511", start="2026-09-09T11:00:00+08:00",
              end="2026-09-09T11:45:00+08:00", charging=0),
    ]
    rec = by_id(allocate(rules, requests)[0])["REQ-ARM-511"]
    assert rec["decision"] == "allocated"


def test_tail_into_maintenance_is_preempted(rules):
    # Activity 11:30-12:00 is clear, but the default 15-minute tail reaches
    # 12:15, inside the 12:00-13:00 window.
    requests = [
        draft("REQ-ARM-512", start="2026-09-09T11:30:00+08:00",
              end="2026-09-09T12:00:00+08:00", charging=0),
    ]
    rec = by_id(allocate(rules, requests)[0])["REQ-ARM-512"]
    assert rec["decision"] == "conflict"
    assert rec["conflict_code"] == errors.MAINTENANCE_WINDOW
    assert rec["next_available_at"] == "2026-09-09T13:00:00+08:00"


def test_booking_starting_exactly_when_maintenance_ends_is_allowed(rules):
    requests = [
        draft("REQ-ARM-513", start="2026-09-09T13:00:00+08:00",
              end="2026-09-09T13:30:00+08:00"),
    ]
    rec = by_id(allocate(rules, requests)[0])["REQ-ARM-513"]
    assert rec["decision"] == "allocated"


# ------------------------------------------------------------- turnaround
def test_default_turnaround_gap_is_deferred(rules):
    requests = [
        draft("REQ-ARM-601", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T10:00:00+08:00", charging=0),
        draft("REQ-ARM-602", start="2026-09-09T10:10:00+08:00",
              end="2026-09-09T10:40:00+08:00"),
    ]
    rec = by_id(allocate(rules, requests)[0])["REQ-ARM-602"]
    assert rec["decision"] == "deferred"
    assert rec["conflict_code"] == errors.TURNAROUND_REQUIRED
    assert rec["conflicting_resources"] == ["device:ARM-07", "operator:OP-101"]
    assert rec["blocking_request_ids"] == ["REQ-ARM-601"]
    assert rec["next_available_at"] == "2026-09-09T10:15:00+08:00"


def test_gap_of_exactly_turnaround_is_allowed(rules):
    requests = [
        draft("REQ-ARM-611", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T10:00:00+08:00", charging=0),
        draft("REQ-ARM-612", start="2026-09-09T10:15:00+08:00",
              end="2026-09-09T10:45:00+08:00"),
    ]
    decisions = by_id(allocate(rules, requests)[0])
    assert decisions["REQ-ARM-612"]["decision"] == "allocated"


def test_charging_extends_the_gap(rules):
    requests = [
        draft("REQ-ARM-621", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T09:30:00+08:00", charging=45),
        draft("REQ-ARM-622", start="2026-09-09T10:00:00+08:00",
              end="2026-09-09T10:20:00+08:00"),
    ]
    rec = by_id(allocate(rules, requests)[0])["REQ-ARM-622"]
    assert rec["decision"] == "deferred"
    # 09:30 + 45 minutes charging = 10:15.
    assert rec["next_available_at"] == "2026-09-09T10:15:00+08:00"


def test_turnaround_tail_blocks_later_held_booking_too(rules):
    # Higher priority late draft is committed first; the earlier lower
    # priority draft must leave room before it.
    requests = [
        draft("REQ-ARM-631", start="2026-09-09T10:00:00+08:00",
              end="2026-09-09T10:30:00+08:00", priority=1),
        draft("REQ-ARM-632", start="2026-09-09T09:40:00+08:00",
              end="2026-09-09T10:00:00+08:00", priority=5),
    ]
    decisions = by_id(allocate(rules, requests)[0])
    assert decisions["REQ-ARM-632"]["decision"] == "allocated"
    rec = decisions["REQ-ARM-631"]
    assert rec["decision"] == "deferred"
    assert rec["next_available_at"] == "2026-09-09T10:15:00+08:00"


def test_operator_overlap_is_its_own_conflict(rules):
    # Same operator OP-202 on two different devices, both placed in the
    # capacity-2 zone ZONE-D: the operator is the only blocked resource.
    requests = [
        draft("REQ-ROVER-641", device="ROVER-02", zone="ZONE-D",
              operator="OP-202", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T10:00:00+08:00"),
        draft("REQ-ARM-642", device="ARM-07", zone="ZONE-D",
              operator="OP-202", start="2026-09-09T09:30:00+08:00",
              end="2026-09-09T10:00:00+08:00"),
    ]
    rec = by_id(allocate(rules, requests)[0])["REQ-ARM-642"]
    assert rec["decision"] == "conflict"
    assert rec["conflict_code"] == errors.OPERATOR_OVERLAP
    assert rec["conflicting_resources"] == ["operator:OP-202"]
    assert rec["blocking_request_ids"] == ["REQ-ROVER-641"]


# --------------------------------------------------------------- tie-break
def test_priority_wins_over_earlier_start(rules):
    requests = [
        draft("REQ-ARM-701", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T10:00:00+08:00", priority=1),
        draft("REQ-ARM-702", start="2026-09-09T09:30:00+08:00",
              end="2026-09-09T10:00:00+08:00", priority=5),
    ]
    decisions = by_id(allocate(rules, requests)[0])
    assert decisions["REQ-ARM-702"]["decision"] == "allocated"
    assert decisions["REQ-ARM-701"]["decision"] == "conflict"
    assert decisions["REQ-ARM-701"]["blocking_request_ids"] == ["REQ-ARM-702"]


def test_same_priority_earlier_start_wins(rules):
    requests = [
        draft("REQ-ARM-711", start="2026-09-09T09:30:00+08:00",
              end="2026-09-09T10:00:00+08:00", priority=3),
        draft("REQ-ARM-712", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T10:00:00+08:00", priority=3),
    ]
    decisions = by_id(allocate(rules, requests)[0])
    assert decisions["REQ-ARM-712"]["decision"] == "allocated"
    assert decisions["REQ-ARM-711"]["decision"] == "conflict"


def test_same_priority_and_start_request_id_breaks_tie(rules):
    common = dict(start="2026-09-09T09:00:00+08:00",
                  end="2026-09-09T10:00:00+08:00", priority=3)
    requests = [draft("REQ-ARM-722", **common), draft("REQ-ARM-721", **common)]
    decisions = by_id(allocate(rules, requests)[0])
    assert decisions["REQ-ARM-721"]["decision"] == "allocated"
    assert decisions["REQ-ARM-722"]["decision"] == "conflict"


def test_batch_results_keep_input_order(rules):
    requests = [
        draft("REQ-ARM-731", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T09:30:00+08:00", priority=1),
        draft("REQ-ARM-732", start="2026-09-09T11:00:00+08:00",
              end="2026-09-09T11:30:00+08:00", priority=5),
        draft("REQ-ARM-733", start="2026-09-09T15:00:00+08:00",
              end="2026-09-09T15:30:00+08:00", priority=3),
    ]
    results, _ = allocate(rules, requests)
    assert [r["request_id"] for r in results] == [
        "REQ-ARM-731",
        "REQ-ARM-732",
        "REQ-ARM-733",
    ]
    assert [r["index"] for r in results] == [0, 1, 2]


# ------------------------------------------------------------ cross-midnight
def test_cross_midnight_draft_never_reaches_engine(rules):
    requests = [
        draft("REQ-ARM-801", start="2026-09-09T23:30:00+08:00",
              end="2026-09-10T00:30:00+08:00"),
        draft("REQ-ARM-802", start="2026-09-10T00:00:00+08:00",
              end="2026-09-10T00:30:00+08:00"),
    ]
    results, validation = allocate(rules, requests)
    decisions = by_id(results)
    assert decisions["REQ-ARM-801"]["decision"] == "rejected"
    assert decisions["REQ-ARM-801"]["error"]["code"] == errors.CROSSES_MIDNIGHT
    # A booking starting exactly at midnight on the next day is legal.
    assert decisions["REQ-ARM-802"]["decision"] == "allocated"


# ------------------------------------------------------------- mixed batch
def test_mixed_batch_bad_drafts_do_not_affect_good_ones(rules):
    requests = [
        draft("REQ-MIX-901", start="2026-09-09T09:00:00+08:00",
              end="2026-09-09T09:45:00+08:00", priority=4),
        draft("REQ-MIX-902", device="GHOST-1", zone="ZONE-C",
              operator="OP-101"),  # unknown device
        "not-even-an-object",
        draft("REQ-MIX-904", start="2026-09-09T15:00:00+08:00",
              end="2026-09-09T15:30:00+08:00", priority=2),
        draft("REQ-MIX-905", start="2026-09-09T16:00:00+08:00",
              end="2026-09-09T16:30:00+08:00", priority=3),
    ]
    results, validation = allocate(rules, requests)
    # Input order preserved including the non-object at index 2.
    assert [r["index"] for r in results] == [0, 1, 2, 3, 4]
    decisions = by_id(results)
    assert decisions["REQ-MIX-901"]["decision"] == "allocated"
    assert decisions["REQ-MIX-902"]["decision"] == "rejected"
    assert decisions["REQ-MIX-902"]["error"]["code"] == errors.UNKNOWN_DEVICE
    assert results[2]["decision"] == "rejected"
    assert results[2]["error"]["code"] == errors.NOT_OBJECT
    assert decisions["REQ-MIX-904"]["decision"] == "allocated"
    assert decisions["REQ-MIX-905"]["decision"] == "allocated"
    assert validation.draft_errors[1].index == 2


def test_structural_rejection_is_stable_and_indexed(rules):
    requests = [
        {"request_id": "REQ-MIX-911"},  # missing fields
        draft("REQ-MIX-912", priority=9),
        draft("REQ-MIX-913", start="2026-09-09T10:00:00+08:00",
              end="2026-09-09T09:00:00+08:00"),
    ]
    results, _ = allocate(rules, requests)
    assert results[0]["error"]["code"] == errors.MISSING_FIELD
    assert results[1]["error"]["code"] == errors.BAD_VALUE_RANGE
    assert results[2]["error"]["code"] == errors.END_NOT_AFTER_START
