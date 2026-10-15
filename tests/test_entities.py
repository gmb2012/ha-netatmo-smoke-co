"""Tests for devices and entities (states from the Phase-0 fixture events)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netatmo_melder.const import DOMAIN, OFFLINE_AFTER

from .conftest import (
    ACCESS_TOKEN,
    EG,
    HUSH_ID,
    KELLER,
    NCO,
    OG,
    REFRESH_TOKEN,
    SMOKE_OFF_ID,
    SMOKE_ON_ID,
    SMOKE_ON_TIME,
    USER_ID,
    events_without,
    setup_entry,
    tick,
)

EG_PREFIX = "smarter_rauchmelder_eg"
NCO_PREFIX = "smarter_kohlenmonoxidmelder"
NOW = datetime.fromtimestamp(SMOKE_ON_TIME, UTC) + timedelta(minutes=5)

pytestmark = pytest.mark.usefixtures("setup_credentials")


@pytest.fixture(autouse=True)
def frozen_now(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(NOW)
    return freezer


async def test_devices(
    hass: HomeAssistant, config_entry: MockConfigEntry, loaded: dict[str, AsyncMock]
) -> None:
    registry = dr.async_get(hass)
    expected = {
        KELLER: ("Smarter Rauchmelder Keller", "NSD", "2005"),
        OG: ("Smarter Rauchmelder OG", "NSD", "2005"),
        EG: ("Smarter Rauchmelder EG", "NSD", "2005"),
        NCO: ("Smarter Kohlenmonoxidmelder", "NCO", "1100"),
    }
    for module_id, (name, model, firmware) in expected.items():
        device = registry.async_get_device({(DOMAIN, module_id)})
        assert device is not None, module_id
        assert device.name == name
        assert device.manufacturer == "Netatmo"
        assert device.model == model
        assert device.sw_version == firmware
    service = registry.async_get_device({(DOMAIN, USER_ID)})
    assert service is not None
    assert service.entry_type is dr.DeviceEntryType.SERVICE


async def test_entities_per_model(
    hass: HomeAssistant, config_entry: MockConfigEntry, loaded: dict[str, AsyncMock]
) -> None:
    nsd = [
        f"binary_sensor.{EG_PREFIX}_smoke",
        f"binary_sensor.{EG_PREFIX}_tamper",
        f"binary_sensor.{EG_PREFIX}_battery",
        f"binary_sensor.{EG_PREFIX}_detection_chamber",
        f"binary_sensor.{EG_PREFIX}_connectivity",
        f"sensor.{EG_PREFIX}_last_event",
        f"sensor.{EG_PREFIX}_wifi_strength",
        f"sensor.{EG_PREFIX}_last_seen",
        f"button.{EG_PREFIX}_acknowledge_alarm",
    ]
    nco = [
        f"binary_sensor.{NCO_PREFIX}_carbon_monoxide",
        f"binary_sensor.{NCO_PREFIX}_tamper",
        f"binary_sensor.{NCO_PREFIX}_battery",
        f"binary_sensor.{NCO_PREFIX}_connectivity",
        f"sensor.{NCO_PREFIX}_last_event",
        f"sensor.{NCO_PREFIX}_wifi_strength",
        f"sensor.{NCO_PREFIX}_last_seen",
        f"button.{NCO_PREFIX}_acknowledge_alarm",
    ]
    for entity_id in [*nsd, *nco, "sensor.netatmo_melder_last_webhook_received"]:
        assert hass.states.get(entity_id) is not None, entity_id
    # Smoke and chamber only on NSD, CO only on NCO.
    assert hass.states.get(f"binary_sensor.{NCO_PREFIX}_smoke") is None
    assert hass.states.get(f"binary_sensor.{NCO_PREFIX}_detection_chamber") is None
    assert hass.states.get(f"binary_sensor.{EG_PREFIX}_carbon_monoxide") is None
    assert hass.states.get(f"binary_sensor.{EG_PREFIX}_smoke").attributes["device_class"] == (
        "smoke"
    )
    assert (
        hass.states.get(f"binary_sensor.{NCO_PREFIX}_carbon_monoxide").attributes["device_class"]
        == "carbon_monoxide"
    )

    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    # 3 NSD x 9 + 1 NCO x 8 + last webhook
    assert len(entries) == 3 * 9 + 8 + 1


async def test_unique_ids(
    hass: HomeAssistant, config_entry: MockConfigEntry, loaded: dict[str, AsyncMock]
) -> None:
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    module_ids = (KELLER, OG, EG, NCO)
    for entry in entries:
        assert ACCESS_TOKEN not in entry.unique_id
        assert REFRESH_TOKEN not in entry.unique_id
        assert entry.unique_id.startswith((*module_ids, USER_ID)), entry.unique_id
    assert registry.async_get(f"binary_sensor.{EG_PREFIX}_smoke").unique_id == f"{EG}_smoke"


async def test_states_after_real_alarm_sequence(
    hass: HomeAssistant, loaded: dict[str, AsyncMock]
) -> None:
    """13.10.: smoke 1, hush 1, smoke 0 -> no alarm, last event "Rauch abgezogen"."""
    assert hass.states.get(f"binary_sensor.{EG_PREFIX}_smoke").state == STATE_OFF
    for key in ("tamper", "battery", "detection_chamber"):
        assert hass.states.get(f"binary_sensor.{EG_PREFIX}_{key}").state == STATE_OFF
    last = hass.states.get(f"sensor.{EG_PREFIX}_last_event")
    assert last.state == "Smarter Rauchmelder EG: Rauch abgezogen"
    assert last.attributes["type"] == "smoke"
    assert last.attributes["sub_type"] == 0
    assert last.attributes["event_id"] == SMOKE_OFF_ID
    assert hass.states.get(f"sensor.{EG_PREFIX}_wifi_strength").state == "62"
    assert hass.states.get(f"sensor.{EG_PREFIX}_last_seen").state == "2026-10-13T16:05:45+00:00"
    # No event for the CO alarm in the fixture.
    assert hass.states.get(f"sensor.{NCO_PREFIX}_last_event").state == STATE_UNKNOWN
    assert hass.states.get(f"binary_sensor.{NCO_PREFIX}_carbon_monoxide").state == STATE_OFF


async def test_alarm_on_and_acknowledge(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    frozen_now: FrozenDateTimeFactory,
) -> None:
    smoke = f"binary_sensor.{EG_PREFIX}_smoke"
    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    await setup_entry(hass, config_entry)

    state = hass.states.get(smoke)
    assert state.state == STATE_ON
    assert state.attributes["event_id"] == SMOKE_ON_ID
    assert state.attributes["alarm_since"] == datetime.fromtimestamp(SMOKE_ON_TIME, UTC)
    assert state.attributes["hushed_until"] == datetime.fromtimestamp(SMOKE_ON_TIME + 900, UTC)

    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: f"button.{EG_PREFIX}_acknowledge_alarm"},
        blocking=True,
    )
    assert hass.states.get(smoke).state == STATE_OFF

    # The same events again (poll or webhook) do not bring the acknowledged alarm back.
    await tick(hass, frozen_now)
    assert hass.states.get(smoke).state == STATE_OFF


async def test_new_alarm_after_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    frozen_now: FrozenDateTimeFactory,
) -> None:
    mock_api["getevents"].return_value = events_without(SMOKE_ON_ID, HUSH_ID, SMOKE_OFF_ID)
    await setup_entry(hass, config_entry)
    smoke = f"binary_sensor.{EG_PREFIX}_smoke"
    assert hass.states.get(smoke).state == STATE_OFF

    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    await tick(hass, frozen_now)
    assert hass.states.get(smoke).state == STATE_ON
    # Other detectors are unaffected.
    assert hass.states.get("binary_sensor.smarter_rauchmelder_keller_smoke").state == STATE_OFF

    mock_api["getevents"].return_value = events_without()
    await tick(hass, frozen_now)
    assert hass.states.get(smoke).state == STATE_OFF


async def test_co_alarm(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
) -> None:
    mock_api["getevents"].return_value = [
        {"id": "co1", "module_id": NCO, "type": "co_detected", "sub_type": 1, "time": 100},
        {"id": "t1", "module_id": NCO, "type": "tampered", "sub_type": 1, "time": 101},
        {"id": "b1", "module_id": NCO, "type": "battery_status", "sub_type": 0, "time": 102},
    ]
    await setup_entry(hass, config_entry)
    assert hass.states.get(f"binary_sensor.{NCO_PREFIX}_carbon_monoxide").state == STATE_ON
    assert hass.states.get(f"binary_sensor.{NCO_PREFIX}_tamper").state == STATE_ON
    assert hass.states.get(f"binary_sensor.{NCO_PREFIX}_battery").state == STATE_ON
    assert hass.states.get(f"binary_sensor.{EG_PREFIX}_smoke").state == STATE_OFF


@pytest.mark.parametrize(
    ("since_last_seen", "expected"),
    [
        (timedelta(days=1), STATE_ON),
        (OFFLINE_AFTER, STATE_ON),
        (OFFLINE_AFTER + timedelta(seconds=1), STATE_OFF),
    ],
)
async def test_connectivity_threshold(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    frozen_now: FrozenDateTimeFactory,
    since_last_seen: timedelta,
    expected: str,
) -> None:
    og_last_seen = 1790894548
    frozen_now.move_to(datetime.fromtimestamp(og_last_seen, UTC) + since_last_seen)
    await setup_entry(hass, config_entry)
    assert timedelta(days=45) == OFFLINE_AFTER
    assert hass.states.get("binary_sensor.smarter_rauchmelder_og_connectivity").state == expected


async def test_missing_status_is_unknown(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: dict[str, AsyncMock]
) -> None:
    mock_api["homestatus"].return_value = [{"id": EG, "type": "NSD"}, "garbage"]
    await setup_entry(hass, config_entry)
    assert hass.states.get(f"binary_sensor.{EG_PREFIX}_connectivity").state == STATE_UNKNOWN
    assert hass.states.get(f"sensor.{EG_PREFIX}_last_seen").state == STATE_UNKNOWN
    assert hass.states.get(f"sensor.{EG_PREFIX}_wifi_strength").state == STATE_UNKNOWN
    assert hass.states.get(f"binary_sensor.{NCO_PREFIX}_connectivity").state == STATE_UNKNOWN
