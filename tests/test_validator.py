"""Per-draft and envelope validation: references, compatibility, bad shapes."""

from app import errors

from .conftest import draft, envelope


def _validate_one(rules, raw_draft):
    from app.validator import validate_draft

    return validate_draft(raw_draft, 0, rules, seen_ids=set())


def test_unknown_device_rejected(rules):
    outcome = _validate_one(rules, draft("REQ-BAD-001", device="ARM-99"))
    assert outcome.code == errors.UNKNOWN_DEVICE


def test_unknown_zone_rejected(rules):
    outcome = _validate_one(rules, draft("REQ-BAD-002", zone="ZONE-Z"))
    assert outcome.code == errors.UNKNOWN_ZONE


def test_unknown_operator_rejected(rules):
    outcome = _validate_one(rules, draft("REQ-BAD-003", operator="OP-000"))
    assert outcome.code == errors.UNKNOWN_OPERATOR


def test_device_zone_incompatible(rules):
    outcome = _validate_one(rules, draft("REQ-BAD-004", device="ROVER-02",
                                         zone="ZONE-C", operator="OP-202"))
    assert outcome.code == errors.DEVICE_ZONE_INCOMPATIBLE
    assert outcome.field == "zone_id"


def test_operator_not_authorized(rules):
    outcome = _validate_one(rules, draft("REQ-BAD-005", device="ROVER-02",
                                         zone="ZONE-D", operator="OP-101"))
    assert outcome.code == errors.OPERATOR_NOT_AUTHORIZED


def test_authorized_combo_passes(rules):
    from app.validator import ValidDraft

    outcome = _validate_one(
        rules, draft("REQ-OK-001", device="ROVER-02", zone="ZONE-D",
                     operator="OP-202")
    )
    assert isinstance(outcome, ValidDraft)


def test_wrong_offset_is_redirected(rules):
    bad = draft("REQ-BAD-006", start="2026-09-09T09:00:00+00:00")
    assert _validate_one(rules, bad).code == errors.TIMEZONE_MISMATCH


def test_cross_midnight_draft_rejected(rules):
    bad = draft("REQ-BAD-007", start="2026-09-09T23:30:00+08:00",
                end="2026-09-10T00:30:00+08:00")
    assert _validate_one(rules, bad).code == errors.CROSSES_MIDNIGHT


def test_envelope_wrong_timezone_is_envelope_error(rules):
    from app.validator import validate_batch

    payload = envelope([draft("REQ-ENV-001")])
    payload["timezone"] = "UTC"
    validation, err = validate_batch(payload, rules)
    assert validation is None
    assert err.code == errors.BAD_TIMEZONE


def test_envelope_illegal_timezone_type(rules):
    from app.validator import validate_batch

    payload = envelope([draft("REQ-ENV-002")])
    payload["timezone"] = 8
    validation, err = validate_batch(payload, rules)
    assert validation is None
    assert err.code == errors.BAD_TYPE


def test_unexpected_envelope_and_draft_fields(rules):
    from app.validator import validate_batch

    payload = envelope([draft("REQ-ENV-003")])
    payload["extra"] = 1
    _, err = validate_batch(payload, rules)
    assert err.code == errors.UNEXPECTED_FIELD

    bad_draft = draft("REQ-ENV-004")
    bad_draft["mystery"] = True
    outcome = _validate_one(rules, bad_draft)
    assert outcome.code == errors.UNEXPECTED_FIELD


def test_duplicate_request_id_in_batch(rules):
    from app.validator import validate_batch

    payload = envelope([draft("REQ-DUP-001"), draft("REQ-DUP-001")])
    validation, err = validate_batch(payload, rules)
    assert err is None
    assert len(validation.valid) == 1
    assert validation.draft_errors[0].code == errors.DUPLICATE_REQUEST_ID


def test_non_object_draft(rules):
    from app.validator import validate_batch

    payload = envelope(["nope"])
    validation, err = validate_batch(payload, rules)
    assert err is None
    assert validation.draft_errors[0].code == errors.NOT_OBJECT


def test_empty_batch_rejected_at_envelope(rules):
    from app.validator import validate_batch

    _, err = validate_batch(envelope([]), rules)
    assert err.code == errors.BAD_LENGTH
