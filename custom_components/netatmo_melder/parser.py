"""Pure functions: Netatmo events -> detector state. No Home Assistant imports.

Event fields (getevents, verified in Phase 0): id, module_id, type, sub_type, time, message.
Semantics verified with a real alarm on 13.10.2026 (NSD):
  smoke    sub_type 1 = smoke detected, 0 = smoke cleared (the alarm ends with its own event)
  hush     sub_type 1 = muted for 15 min (does NOT end the alarm)
  wifi_status sub_type 1 = connected
Not seen in Phase 0, taken from the public Netatmo event list (unverified):
  co_detected (NCO) assumed analogous to smoke; any sub_type >= 1 counts as alarm so that a
  possible pre-alarm/alarm level is never missed (CO path untested, no test gas available)
  tampered 1 = tampered, 0 = mounted again; detection_chamber_status 1 = dirty, 0 = clean;
  battery_status 0 = low, 1 = very low (both mean "low", there is no "battery ok" event);
  wifi_status 0 = failed; sound_test 0 = ok, 1 = failed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields, replace
import json
from typing import Any

HUSH_SECONDS = 15 * 60
# getevents returns the latest ~30 events; keep far more ids than that for deduplication.
MAX_PROCESSED_IDS = 1000

ALARM_TYPES = frozenset({"smoke", "co_detected"})
# event type -> state field set to (sub_type in true_sub_types)
_FLAG_TYPES: dict[str, tuple[str, frozenset[int]]] = {
    "tampered": ("tampered", frozenset({1})),
    "battery_status": ("battery_low", frozenset({0, 1})),
    "detection_chamber_status": ("chamber_dirty", frozenset({1})),
    "wifi_status": ("wifi_ok", frozenset({1})),
    "sound_test": ("sound_test_failed", frozenset({1})),
}
KNOWN_TYPES = ALARM_TYPES | {"hush"} | frozenset(_FLAG_TYPES)


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    module_id: str
    type: str
    sub_type: int | None
    time: int
    message: str


@dataclass(frozen=True, slots=True)
class DetectorState:
    alarm: bool = False
    alarm_since: int | None = None
    alarm_event_id: str | None = None
    # time of the newest smoke/co_detected event applied; older ones must not override it
    alarm_updated: int | None = None
    hushed_until: int | None = None
    tampered: bool = False
    battery_low: bool = False
    chamber_dirty: bool = False
    wifi_ok: bool | None = None
    sound_test_failed: bool = False
    last_event_id: str | None = None
    last_event_type: str | None = None
    last_event_sub_type: int | None = None
    last_event_time: int | None = None
    last_event_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DetectorState:
        names = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})


@dataclass(frozen=True, slots=True)
class EventBatch:
    states: dict[str, DetectorState]
    processed: dict[str, int]
    applied: tuple[Event, ...]
    # alarm events the state accepted (not ignored as older than the detector's last
    # smoke/CO event); these are the ones to push
    alarms: tuple[Event, ...] = ()


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_event(raw: Any) -> Event | None:
    """Return an Event, or None when required fields are missing or malformed."""
    if not isinstance(raw, Mapping):
        return None
    event_id, module_id, type_ = raw.get("id"), raw.get("module_id"), raw.get("type")
    time = _int(raw.get("time"))
    if not (isinstance(event_id, str) and isinstance(module_id, str) and isinstance(type_, str)):
        return None
    if time is None:
        return None
    message = raw.get("message")
    return Event(
        id=event_id,
        module_id=module_id,
        type=type_,
        sub_type=_int(raw.get("sub_type")),
        time=time,
        message=message if isinstance(message, str) else "",
    )


def is_alarm(event: Event) -> bool:
    """True for smoke/CO detected (the events that must trigger a push)."""
    return event.type in ALARM_TYPES and event.sub_type is not None and event.sub_type >= 1


def _with_last_event(state: DetectorState, event: Event) -> DetectorState:
    if state.last_event_time is not None and event.time < state.last_event_time:
        return state
    return replace(
        state,
        last_event_id=event.id,
        last_event_type=event.type,
        last_event_sub_type=event.sub_type,
        last_event_time=event.time,
        last_event_message=event.message,
    )


def _apply_alarm(state: DetectorState, event: Event) -> DetectorState:
    if state.alarm_updated is not None and event.time < state.alarm_updated:
        return state
    if is_alarm(event):
        if state.alarm:
            return replace(state, alarm_updated=event.time)
        return replace(
            state,
            alarm=True,
            alarm_since=event.time,
            alarm_event_id=event.id,
            alarm_updated=event.time,
        )
    return replace(
        state,
        alarm=False,
        alarm_since=None,
        alarm_event_id=None,
        alarm_updated=event.time,
        hushed_until=None,
    )


def apply_event(state: DetectorState, event: Event) -> DetectorState:
    """Return the state after one event of this detector; unknown types change nothing."""
    if event.type not in KNOWN_TYPES:
        return state
    new = _with_last_event(state, event)
    if event.sub_type is None:
        return new
    if event.type in ALARM_TYPES:
        return _apply_alarm(new, event)
    if event.type == "hush":
        hushed = event.time + HUSH_SECONDS if event.sub_type == 1 else None
        return replace(new, hushed_until=hushed)
    field, true_sub_types = _FLAG_TYPES[event.type]
    return replace(new, **{field: event.sub_type in true_sub_types})


def acknowledge(state: DetectorState) -> DetectorState:
    """Reset the alarm locally (button), keep everything else."""
    return replace(state, alarm=False, alarm_since=None, alarm_event_id=None, hushed_until=None)


def apply_events(
    states: Mapping[str, DetectorState],
    raw_events: Iterable[Any],
    processed: Mapping[str, int],
    module_ids: frozenset[str],
) -> EventBatch:
    """Apply new events (home-wide list) to the detectors in module_ids.

    Events of other modules are skipped (getevents does not filter per detector), already
    processed ids are applied only once, and events are applied in time order.
    """
    new_states = dict(states)
    new_processed = dict(processed)
    pending: dict[str, Event] = {}
    for raw in raw_events:
        event = parse_event(raw)
        if event is None or event.module_id not in module_ids or event.id in new_processed:
            continue
        pending[event.id] = event

    applied: list[Event] = []
    alarms: list[Event] = []
    for event in sorted(pending.values(), key=lambda e: (e.time, e.id)):
        new_processed[event.id] = event.time
        if event.type not in KNOWN_TYPES:
            continue
        current = new_states.get(event.module_id, DetectorState())
        new_state = apply_event(current, event)
        new_states[event.module_id] = new_state
        applied.append(event)
        if is_alarm(event) and new_state.alarm_updated == event.time:
            alarms.append(event)

    if len(new_processed) > MAX_PROCESSED_IDS:
        newest = sorted(new_processed.items(), key=lambda item: item[1], reverse=True)
        new_processed = dict(newest[:MAX_PROCESSED_IDS])
    return EventBatch(
        states=new_states,
        processed=new_processed,
        applied=tuple(applied),
        alarms=tuple(alarms),
    )


def parse_webhook_payload(raw: bytes) -> dict[str, Any] | None:
    """Decode a webhook body; None for anything that is not a JSON object."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
