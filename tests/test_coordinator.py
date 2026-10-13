"""Tests for the coordinator: polling, persistence, deduplication, alarm event, errors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.netatmo_melder.api import NetatmoAuthError, NetatmoConnectionError
from custom_components.netatmo_melder.const import (
    DOMAIN,
    EVENT_ALARM,
    SCAN_INTERVAL,
    WEBHOOK_SAVE_DELAY,
)

from .conftest import (
    EG,
    HUSH_ID,
    SMOKE_OFF_ID,
    SMOKE_ON_ID,
    SMOKE_ON_TIME,
    events_without,
    fixture_events,
    setup_entry,
    tick,
)

SMOKE = "binary_sensor.smarter_rauchmelder_eg_smoke"
ALARM_AT = datetime.fromtimestamp(SMOKE_ON_TIME, UTC)

pytestmark = pytest.mark.usefixtures("setup_credentials")


def _store_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


async def test_polls_homestatus_and_getevents_home_wide(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, config_entry)
    mock_api["homesdata"].assert_awaited_once()
    mock_api["homestatus"].assert_awaited_once_with("000000000000000000000001")
    mock_api["getevents"].assert_awaited_once_with("000000000000000000000001")

    await tick(hass, freezer)
    # homesdata only once, the rest every 5 minutes.
    mock_api["homesdata"].assert_awaited_once()
    assert mock_api["getevents"].await_count == 2
    assert mock_api["homestatus"].await_count == 2
    assert timedelta(minutes=5) == SCAN_INTERVAL


async def test_fresh_alarm_fires_event_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(ALARM_AT - timedelta(minutes=1))
    mock_api["getevents"].return_value = events_without(SMOKE_ON_ID, HUSH_ID, SMOKE_OFF_ID)
    events = async_capture_events(hass, EVENT_ALARM)
    await setup_entry(hass, config_entry)
    assert hass.states.get(SMOKE).state == "off"

    # Webhook latency ~3.5 min: the alarm shows up in getevents at the next poll.
    freezer.move_to(ALARM_AT + timedelta(seconds=210))
    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    await tick(hass, freezer)

    assert hass.states.get(SMOKE).state == "on"
    assert len(events) == 1
    assert events[0].data == {
        "module_id": EG,
        "name": "Smarter Rauchmelder EG",
        "type": "smoke",
        "event_id": SMOKE_ON_ID,
        "time": ALARM_AT.isoformat(),
        "active": True,
    }

    # Polling again (and the webhook duplicate) must not push twice.
    await tick(hass, freezer)
    assert len(events) == 1

    # Smoke cleared: alarm off, no further alarm event.
    mock_api["getevents"].return_value = fixture_events()
    await tick(hass, freezer)
    assert hass.states.get(SMOKE).state == "off"
    assert len(events) == 1


@pytest.mark.parametrize(
    ("age", "fired"),
    [
        (timedelta(minutes=10), True),
        (timedelta(minutes=10, seconds=1), False),
        (timedelta(days=2), False),
    ],
)
async def test_backfill_only_pushes_recent_alarms(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    age: timedelta,
    fired: bool,
) -> None:
    freezer.move_to(ALARM_AT + age)
    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    events = async_capture_events(hass, EVENT_ALARM)
    await setup_entry(hass, config_entry)

    # The state is set either way, only the push depends on the age.
    assert hass.states.get(SMOKE).state == "on"
    assert len(events) == (1 if fired else 0)
    if not fired:
        assert "state updated, no netatmo_melder_alarm event" in caplog.text


async def test_state_and_processed_ids_are_persisted(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_storage: dict[str, Any],
) -> None:
    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    await setup_entry(hass, config_entry)

    stored = hass_storage[_store_key(config_entry)]["data"]
    assert set(stored["processed"]) == {e["id"] for e in events_without(SMOKE_OFF_ID)}
    assert stored["states"][EG]["alarm"] is True
    assert stored["states"][EG]["alarm_event_id"] == SMOKE_ON_ID
    assert stored["last_webhook"] is None


async def test_restart_does_not_reapply_or_push_again(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(ALARM_AT + timedelta(minutes=4))
    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    events = async_capture_events(hass, EVENT_ALARM)
    await setup_entry(hass, config_entry)
    assert len(events) == 1

    # Acknowledge, then restart (unload + setup): the processed alarm stays acknowledged.
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.smarter_rauchmelder_eg_acknowledge_alarm"},
        blocking=True,
    )
    assert hass.states.get(SMOKE).state == "off"
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SMOKE).state == "off"
    assert len(events) == 1


async def test_stored_state_is_restored(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_storage: dict[str, Any],
) -> None:
    hass_storage[_store_key(config_entry)] = {
        "version": 1,
        "minor_version": 1,
        "key": _store_key(config_entry),
        "data": {
            "processed": {e["id"]: e["time"] for e in fixture_events()},
            "states": {EG: {"alarm": True, "alarm_since": SMOKE_ON_TIME, "tampered": True}},
            "last_webhook": "2026-10-13T16:03:49+00:00",
        },
    }
    await setup_entry(hass, config_entry)

    # All fixture events were processed before the restart: nothing is applied again.
    assert hass.states.get(SMOKE).state == "on"
    assert hass.states.get("binary_sensor.smarter_rauchmelder_eg_tamper").state == "on"
    assert (
        hass.states.get("sensor.netatmo_melder_last_webhook_received").state
        == "2026-10-13T16:03:49+00:00"
    )


async def test_webhook_time_is_persisted(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-13T16:03:49+00:00")
    await setup_entry(hass, config_entry)
    await config_entry.runtime_data.async_webhook_received()
    await hass.async_block_till_done()

    # Written with a delay (no Store write per webhook) ...
    assert hass_storage[_store_key(config_entry)]["data"]["last_webhook"] is None
    await tick(hass, freezer, timedelta(seconds=WEBHOOK_SAVE_DELAY))
    stored = hass_storage[_store_key(config_entry)]["data"]
    assert stored["last_webhook"] == "2026-10-13T16:03:49+00:00"
    assert (
        hass.states.get("sensor.netatmo_melder_last_webhook_received").state
        == "2026-10-13T16:03:49+00:00"
    )


@pytest.mark.parametrize("method", ["homesdata", "homestatus", "getevents"])
async def test_auth_error_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    method: str,
) -> None:
    mock_api[method].side_effect = NetatmoAuthError("401")
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


@pytest.mark.parametrize("method", ["homesdata", "homestatus", "getevents"])
async def test_connection_error_retries_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    method: str,
) -> None:
    mock_api[method].side_effect = NetatmoConnectionError("429")
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_no_detectors_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["homesdata"].return_value = {"homes": [], "user": {"id": "x"}}
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_errors_after_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, config_entry)
    assert hass.states.get(SMOKE).state == "off"

    mock_api["getevents"].side_effect = NetatmoConnectionError("500")
    await tick(hass, freezer)
    assert hass.states.get(SMOKE).state == "unavailable"

    mock_api["getevents"].side_effect = NetatmoAuthError("403")
    await tick(hass, freezer)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]

    # HA stops polling until the reauth is done.
    calls = mock_api["getevents"].await_count
    await tick(hass, freezer)
    assert mock_api["getevents"].await_count == calls


async def test_recovers_after_connection_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, config_entry)
    mock_api["homestatus"].side_effect = NetatmoConnectionError("timeout")
    await tick(hass, freezer)
    assert hass.states.get(SMOKE).state == "unavailable"
    mock_api["homestatus"].side_effect = None
    await tick(hass, freezer)
    assert hass.states.get(SMOKE).state == "off"


async def test_pending_webhook_time_is_saved_on_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-13T16:03:49+00:00")
    await setup_entry(hass, config_entry)
    await config_entry.runtime_data.async_webhook_received()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[_store_key(config_entry)]["data"]
    assert stored["last_webhook"] == "2026-10-13T16:03:49+00:00"


@pytest.mark.parametrize(
    ("age", "fired"),
    [
        (timedelta(minutes=9), True),
        (timedelta(minutes=11), False),
    ],
)
async def test_backfill_gate_on_first_refresh(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
    age: timedelta,
    fired: bool,
) -> None:
    freezer.move_to(ALARM_AT + age)
    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    events = async_capture_events(hass, EVENT_ALARM)
    await setup_entry(hass, config_entry)
    assert hass.states.get(SMOKE).state == "on"
    assert len(events) == (1 if fired else 0)


async def test_live_alarm_is_pushed_however_late(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """After the first refresh every newly seen alarm is pushed (polling only, missed poll)."""
    freezer.move_to(ALARM_AT - timedelta(minutes=1))
    mock_api["getevents"].return_value = events_without(SMOKE_ON_ID, HUSH_ID, SMOKE_OFF_ID)
    events = async_capture_events(hass, EVENT_ALARM)
    await setup_entry(hass, config_entry)

    freezer.move_to(ALARM_AT + timedelta(minutes=15))
    mock_api["getevents"].return_value = fixture_events()  # alarm already over when seen
    await tick(hass, freezer)
    assert [event.data["event_id"] for event in events] == [SMOKE_ON_ID]
    assert events[0].data["active"] is False  # alarm had already ended when it was seen
    assert hass.states.get(SMOKE).state == "off"


async def test_backfill_flag_survives_failed_first_refresh(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed first refresh (setup retry) keeps the gate for the next first refresh."""
    freezer.move_to(ALARM_AT + timedelta(hours=1))
    mock_api["getevents"].side_effect = NetatmoConnectionError("down")
    events = async_capture_events(hass, EVENT_ALARM)
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    mock_api["getevents"].side_effect = None
    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SMOKE).state == "on"
    assert events == []


async def test_ignored_older_alarm_is_not_pushed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A late-arriving smoke-1 older than an already applied smoke-0 sets nothing, no push."""
    freezer.move_to(ALARM_AT + timedelta(minutes=10))
    mock_api["getevents"].return_value = events_without(SMOKE_ON_ID, HUSH_ID)
    events = async_capture_events(hass, EVENT_ALARM)
    await setup_entry(hass, config_entry)

    mock_api["getevents"].return_value = fixture_events()
    await tick(hass, freezer)
    assert hass.states.get(SMOKE).state == "off"
    assert events == []


async def test_no_timers_after_shutdown(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_entry(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    calls = mock_api["getevents"].await_count

    await coordinator.async_webhook_received("late-event")
    coordinator._check_awaited_events(2)
    assert coordinator._unsub_throttled is None
    assert coordinator._unsub_retry is None
    await tick(hass, freezer)
    assert mock_api["getevents"].await_count == calls


async def test_remove_entry_removes_store(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_storage: dict[str, Any],
) -> None:
    await setup_entry(hass, config_entry)
    assert _store_key(config_entry) in hass_storage
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _store_key(config_entry) not in hass_storage
