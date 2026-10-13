"""Sensors per detector (last event, WiFi, last seen) and for the account (last webhook)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import Module, NetatmoMelderConfigEntry, NetatmoMelderCoordinator
from .entity import NetatmoMelderAccountEntity, NetatmoMelderEntity
from .parser import DetectorState


def _ts(value: Any) -> datetime | None:
    return dt_util.utc_from_timestamp(value) if isinstance(value, int) else None


def _last_event_attributes(state: DetectorState) -> dict[str, Any]:
    return {
        "type": state.last_event_type,
        "sub_type": state.last_event_sub_type,
        "time": _ts(state.last_event_time),
        "event_id": state.last_event_id,
    }


@dataclass(frozen=True, kw_only=True)
class MelderSensorDescription(SensorEntityDescription):
    value_fn: Callable[[DetectorState, dict[str, Any]], Any]
    attributes_fn: Callable[[DetectorState], dict[str, Any]] | None = None


SENSORS = (
    MelderSensorDescription(
        key="last_event",
        translation_key="last_event",
        value_fn=lambda state, _: state.last_event_message or state.last_event_type,
        attributes_fn=_last_event_attributes,
    ),
    MelderSensorDescription(
        key="wifi_strength",
        translation_key="wifi_strength",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, status: status.get("wifi_strength"),
    ),
    MelderSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, status: _ts(status.get("last_seen")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetatmoMelderConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        MelderSensor(coordinator, module, description)
        for module in coordinator.modules.values()
        for description in SENSORS
    ]
    entities.append(LastWebhookSensor(coordinator, "last_webhook"))
    async_add_entities(entities)


class MelderSensor(NetatmoMelderEntity, SensorEntity):
    entity_description: MelderSensorDescription

    def __init__(
        self,
        coordinator: NetatmoMelderCoordinator,
        module: Module,
        description: MelderSensorDescription,
    ) -> None:
        super().__init__(coordinator, module, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.detector, dict(self.status))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.detector)


class LastWebhookSensor(NetatmoMelderAccountEntity, SensorEntity):
    _attr_translation_key = "last_webhook"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.last_webhook
