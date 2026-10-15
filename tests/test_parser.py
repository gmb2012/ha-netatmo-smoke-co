"""Tests for the pure event parser (no Home Assistant involved)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from custom_components.netatmo_melder.parser import (
    HUSH_SECONDS,
    MAX_PROCESSED_IDS,
    DetectorState,
    Event,
    acknowledge,
    apply_event,
    apply_events,
    expire_alarm,
    is_alarm,
    parse_event,
    parse_webhook_payload,
)

from .conftest import (
    EG,
    HUSH_ID,
    KELLER,
    NCO,
    OG,
    SMOKE_OFF_ID,
    SMOKE_ON_ID,
    SMOKE_ON_TIME,
    fixture_events,
    load_fixture,
)

ALL_MODULES = frozenset({KELLER, OG, EG, NCO})
SMOKE_OFF_TIME = 1791907547


def _by_id(event_id: str) -> dict[str, Any]:
    return next(e for e in fixture_events() if e["id"] == event_id)


def _event(type_: str, sub_type: int | None, time: int = 1000, module_id: str = EG) -> Event:
    return Event(
        id=f"{type_}-{sub_type}-{time}",
        module_id=module_id,
        type=type_,
        sub_type=sub_type,
        time=time,
        message=f"{type_} {sub_type}",
    )


def test_parser_has_no_homeassistant_import() -> None:
    import custom_components.netatmo_melder.parser as parser

    source = Path(parser.__file__).read_text(encoding="utf-8")
    assert "homeassistant" not in source
    assert parser.__name__ in sys.modules


def test_parse_event_from_fixture() -> None:
    event = parse_event(_by_id(SMOKE_ON_ID))
    assert event == Event(
        id=SMOKE_ON_ID,
        module_id=EG,
        type="smoke",
        sub_type=1,
        time=SMOKE_ON_TIME,
        message="Smarter Rauchmelder EG: Rauch erkannt",
    )


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"id": "x", "module_id": EG, "type": "smoke"},
        {"id": "x", "module_id": EG, "time": 1},
        {"id": "x", "type": "smoke", "time": 1},
        {"module_id": EG, "type": "smoke", "time": 1},
        {"id": "x", "module_id": EG, "type": "smoke", "time": "soon"},
        {"id": "x", "module_id": EG, "type": "smoke", "time": True},
        "not a dict",
        None,
    ],
)
def test_parse_event_rejects_incomplete(raw: Any) -> None:
    assert parse_event(raw) is None


def test_parse_event_tolerates_missing_or_odd_sub_type() -> None:
    base = {"id": "x", "module_id": EG, "type": "smoke", "time": 5}
    assert parse_event(base).sub_type is None
    assert parse_event({**base, "sub_type": "1"}).sub_type is None
    assert parse_event({**base, "sub_type": True}).sub_type is None
    assert parse_event(base).message == ""


def test_smoke_on_and_off() -> None:
    state = apply_event(DetectorState(), _event("smoke", 1, time=100))
    assert state.alarm is True
    assert state.alarm_since == 100
    assert state.alarm_event_id == "smoke-1-100"

    state = apply_event(state, _event("smoke", 0, time=200))
    assert state.alarm is False
    assert state.alarm_since is None
    assert state.alarm_event_id is None


def test_co_detected_on_and_off() -> None:
    state = apply_event(DetectorState(), _event("co_detected", 1, time=100, module_id=NCO))
    assert state.alarm is True
    state = apply_event(state, _event("co_detected", 0, time=200, module_id=NCO))
    assert state.alarm is False


def test_co_detected_higher_sub_type_is_alarm() -> None:
    # Unverified: some Netatmo docs list a CO pre-alarm/alarm level; never miss an alarm.
    state = apply_event(DetectorState(), _event("co_detected", 2, time=100, module_id=NCO))
    assert state.alarm is True


def test_hush_does_not_end_alarm() -> None:
    state = apply_event(DetectorState(), _event("smoke", 1, time=100))
    state = apply_event(state, _event("hush", 1, time=100))
    assert state.alarm is True
    assert state.hushed_until == 100 + HUSH_SECONDS
    assert HUSH_SECONDS == 15 * 60

    state = apply_event(state, _event("hush", 0, time=150))
    assert state.alarm is True
    assert state.hushed_until is None


def test_alarm_end_clears_hush() -> None:
    state = apply_event(DetectorState(), _event("smoke", 1, time=100))
    state = apply_event(state, _event("hush", 1, time=100))
    state = apply_event(state, _event("smoke", 0, time=300))
    assert state.alarm is False
    assert state.hushed_until is None


@pytest.mark.parametrize(
    ("type_", "sub_type", "field", "expected"),
    [
        ("tampered", 1, "tampered", True),
        ("tampered", 0, "tampered", False),
        ("battery_status", 0, "battery_low", True),
        ("battery_status", 1, "battery_low", True),
        ("detection_chamber_status", 1, "chamber_dirty", True),
        ("detection_chamber_status", 0, "chamber_dirty", False),
        ("wifi_status", 1, "wifi_ok", True),
        ("wifi_status", 0, "wifi_ok", False),
        ("sound_test", 0, "sound_test_failed", False),
        ("sound_test", 1, "sound_test_failed", True),
    ],
)
def test_status_events(type_: str, sub_type: int, field: str, expected: bool) -> None:
    start = DetectorState(tampered=not expected, chamber_dirty=not expected)
    state = apply_event(start, _event(type_, sub_type))
    assert getattr(state, field) is expected
    assert state.alarm is False
    assert state.last_event_type == type_
    assert state.last_event_sub_type == sub_type


def test_unknown_type_is_ignored() -> None:
    start = DetectorState(alarm=True, alarm_since=5)
    assert apply_event(start, _event("new_feature", 1)) == start


def test_event_with_unusable_sub_type_keeps_state_but_updates_last_event() -> None:
    start = DetectorState(alarm=True, alarm_since=5, alarm_event_id="a")
    state = apply_event(start, _event("smoke", None, time=50))
    assert state.alarm is True
    assert state.last_event_type == "smoke"
    assert state.last_event_time == 50


def test_older_alarm_event_does_not_override_newer_state() -> None:
    state = apply_event(DetectorState(), _event("smoke", 0, time=300))
    state = apply_event(state, _event("smoke", 1, time=100))
    assert state.alarm is False
    # The newer event stays the "last event".
    assert state.last_event_time == 300
    assert state.last_event_sub_type == 0


def test_last_event_fields() -> None:
    state = apply_event(DetectorState(), parse_event(_by_id(SMOKE_OFF_ID)))
    assert state.last_event_id == SMOKE_OFF_ID
    assert state.last_event_type == "smoke"
    assert state.last_event_sub_type == 0
    assert state.last_event_time == SMOKE_OFF_TIME
    assert state.last_event_message == "Smarter Rauchmelder EG: Rauch abgezogen"


def test_acknowledge_resets_alarm_only() -> None:
    state = DetectorState(
        alarm=True, alarm_since=10, alarm_event_id="a", hushed_until=20, tampered=True
    )
    acked = acknowledge(state)
    assert acked.alarm is False
    assert acked.alarm_since is None
    assert acked.alarm_event_id is None
    assert acked.hushed_until is None
    assert acked.tampered is True
    # Immutable: the original is unchanged.
    assert state.alarm is True


@pytest.mark.parametrize(
    ("now", "expected_alarm"),
    [
        (1000 + 1799, True),
        (1000 + 1800, False),
    ],
)
def test_expire_alarm_after_timeout_without_new_alarm_event(now: int, expected_alarm: bool) -> None:
    """15.10.2026: Netatmo sent no smoke 0, the alarm must not latch forever."""
    state = DetectorState(
        alarm=True,
        alarm_since=1000,
        alarm_event_id="a",
        alarm_updated=1000,
        hushed_until=1900,
        tampered=True,
    )
    expired = expire_alarm(state, now, 1800)
    assert expired.alarm is expected_alarm
    if not expected_alarm:
        assert expired == acknowledge(state)
        assert expired.tampered is True
        # alarm_updated stays, so older alarm events still cannot set the alarm again.
        assert expired.alarm_updated == 1000
    # Immutable: the original is unchanged.
    assert state.alarm is True


def test_expire_alarm_counts_from_the_newest_alarm_event() -> None:
    state = DetectorState(alarm=True, alarm_since=1000, alarm_updated=2000)
    assert expire_alarm(state, 1000 + 1800, 1800).alarm is True
    assert expire_alarm(state, 2000 + 1800, 1800).alarm is False


def test_expire_alarm_falls_back_to_alarm_since() -> None:
    state = DetectorState(alarm=True, alarm_since=1000)
    assert expire_alarm(state, 1000 + 1799, 1800).alarm is True
    assert expire_alarm(state, 1000 + 1800, 1800).alarm is False


@pytest.mark.parametrize(
    "state",
    [
        DetectorState(),
        DetectorState(alarm=False, alarm_updated=0, tampered=True),
        DetectorState(alarm=True),  # no time known: keep it (manual acknowledge)
    ],
)
def test_expire_alarm_leaves_other_states_unchanged(state: DetectorState) -> None:
    assert expire_alarm(state, 10**10, 1800) is state


@pytest.mark.parametrize(
    ("type_", "sub_type", "expected"),
    [
        ("smoke", 1, True),
        ("smoke", 0, False),
        ("co_detected", 1, True),
        ("co_detected", 0, False),
        ("hush", 1, False),
        ("tampered", 1, False),
        ("smoke", None, False),
    ],
)
def test_is_alarm(type_: str, sub_type: int | None, expected: bool) -> None:
    assert is_alarm(_event(type_, sub_type)) is expected


def test_real_sequence_of_13_october() -> None:
    """smoke 1 + hush 1 (same second), then smoke 0: ends without alarm, hush cleared."""
    result = apply_events({}, fixture_events(), {}, ALL_MODULES)

    eg = result.states[EG]
    assert eg.alarm is False
    assert eg.hushed_until is None
    assert eg.last_event_id == SMOKE_OFF_ID
    assert eg.wifi_ok is None

    applied_ids = [event.id for event in result.applied]
    # Sorted by time (then id), not in the newest-first API order.
    assert applied_ids.index(SMOKE_ON_ID) < applied_ids.index(SMOKE_OFF_ID)
    assert applied_ids.index(HUSH_ID) < applied_ids.index(SMOKE_ON_ID)
    times = [event.time for event in result.applied]
    assert times == sorted(times)

    assert result.states[KELLER].wifi_ok is True
    assert result.states[OG].wifi_ok is True
    assert result.states[KELLER].alarm is False
    assert set(result.processed) == {e["id"] for e in fixture_events()}


def test_partial_sequence_alarm_still_on() -> None:
    events = [e for e in fixture_events() if e["id"] != SMOKE_OFF_ID]
    result = apply_events({}, events, {}, ALL_MODULES)
    eg = result.states[EG]
    assert eg.alarm is True
    assert eg.alarm_since == SMOKE_ON_TIME
    assert eg.alarm_event_id == SMOKE_ON_ID
    assert eg.hushed_until == SMOKE_ON_TIME + HUSH_SECONDS


def test_foreign_module_does_not_change_other_detectors() -> None:
    start = {KELLER: DetectorState(), NCO: DetectorState()}
    result = apply_events(start, fixture_events(), {}, frozenset({KELLER, NCO}))
    assert EG not in result.states
    assert OG not in result.states
    assert result.states[NCO] == DetectorState()
    assert result.states[KELLER].wifi_ok is True
    assert all(event.module_id in {KELLER, NCO} for event in result.applied)
    # Foreign events are not marked processed: a later setup that knows the module applies them.
    assert SMOKE_ON_ID not in result.processed


def test_duplicate_ids_applied_once() -> None:
    first = apply_events({}, fixture_events(), {}, ALL_MODULES)
    again = apply_events(first.states, fixture_events(), first.processed, ALL_MODULES)
    assert again.applied == ()
    assert again.states == first.states

    doubled = apply_events({}, fixture_events() + fixture_events(), {}, ALL_MODULES)
    assert len(doubled.applied) == len(fixture_events())


def test_processed_alarm_not_reapplied_after_acknowledge() -> None:
    events = [e for e in fixture_events() if e["id"] != SMOKE_OFF_ID]
    first = apply_events({}, events, {}, ALL_MODULES)
    acked = {**first.states, EG: acknowledge(first.states[EG])}
    again = apply_events(acked, events, first.processed, ALL_MODULES)
    assert again.states[EG].alarm is False


def test_unknown_and_invalid_events_are_skipped() -> None:
    raw = [
        {"id": "u1", "module_id": EG, "type": "something_new", "sub_type": 1, "time": 10},
        {"id": "bad", "module_id": EG, "type": "smoke"},
        "garbage",
    ]
    result = apply_events({}, raw, {}, ALL_MODULES)
    assert result.applied == ()
    assert result.states.get(EG, DetectorState()) == DetectorState()
    assert "u1" in result.processed


def test_inputs_are_not_mutated() -> None:
    states = {EG: DetectorState()}
    processed: dict[str, int] = {}
    apply_events(states, fixture_events(), processed, ALL_MODULES)
    assert states == {EG: DetectorState()}
    assert processed == {}


def test_processed_ids_are_capped_keeping_newest() -> None:
    old = {f"old{i}": i for i in range(MAX_PROCESSED_IDS)}
    result = apply_events({}, fixture_events(), old, ALL_MODULES)
    assert len(result.processed) == MAX_PROCESSED_IDS
    assert SMOKE_OFF_ID in result.processed
    assert "old0" not in result.processed


def test_state_round_trip() -> None:
    state = apply_events({}, fixture_events(), {}, ALL_MODULES).states[EG]
    assert DetectorState.from_dict(state.as_dict()) == state
    assert DetectorState.from_dict({"alarm": True, "unknown_key": 1}) == DetectorState(alarm=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b'{"push_type": "NSD-smoke", "sub_type": 1}', {"push_type": "NSD-smoke", "sub_type": 1}),
        (b"{not json", None),
        (b"", None),
        (b"[1, 2]", None),
        (b"\xff\xfe", None),
    ],
)
def test_parse_webhook_payload(raw: bytes, expected: dict | None) -> None:
    assert parse_webhook_payload(raw) == expected


def test_parse_webhook_payload_fixtures() -> None:
    payloads = load_fixture("webhook_payloads.json")
    parsed = [parse_webhook_payload(json.dumps(p).encode()) for p in payloads]
    assert [p["push_type"] for p in parsed] == [
        "webhook_activation",
        "NSD-smoke",
        "NSD-hush",
        "NSD-smoke",
    ]


def test_repeated_alarm_keeps_alarm_since() -> None:
    state = apply_event(DetectorState(), _event("smoke", 1, time=100))
    state = apply_event(state, _event("smoke", 1, time=200))
    assert state.alarm is True
    assert state.alarm_since == 100
    assert state.alarm_event_id == "smoke-1-100"


def test_batch_lists_accepted_alarms_only() -> None:
    """An alarm older than the newest smoke/CO event of the detector is not reported."""
    newer_clear = {"id": "clear", "module_id": EG, "type": "smoke", "sub_type": 0, "time": 300}
    first = apply_events({}, [newer_clear], {}, ALL_MODULES)
    assert first.alarms == ()

    late_old_alarm = {"id": "old", "module_id": EG, "type": "smoke", "sub_type": 1, "time": 100}
    second = apply_events(first.states, [late_old_alarm], first.processed, ALL_MODULES)
    assert [event.id for event in second.applied] == ["old"]
    assert second.alarms == ()
    assert second.states[EG].alarm is False


def test_batch_reports_alarm_that_already_ended() -> None:
    """Alarm on and off in the same poll: the alarm is still reported (push even if late)."""
    result = apply_events({}, fixture_events(), {}, ALL_MODULES)
    assert [event.id for event in result.alarms] == [SMOKE_ON_ID]
    assert result.states[EG].alarm is False
