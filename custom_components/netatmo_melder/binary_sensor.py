"""Binary sensors per detector: smoke / CO alarm, tamper, battery, chamber, connectivity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import MODEL_CO, MODEL_SMOKE, OFFLINE_AFTER, SUPPORTED_MODELS
from .coordinator import Module, NetatmoMelderConfigEntry, NetatmoMelderCoordinator
from .entity import NetatmoMelderEntity
from .parser import DetectorState


def _ts(value: int | None) -> datetime | None:
    return dt_util.utc_from_timestamp(value) if value is not None else None


def _online(status: dict[str, Any]) -> bool | None:
    last_seen = status.get("last_seen")
    if not isinstance(last_seen, int):
        return None
    return dt_util.utcnow() - dt_util.utc_from_timestamp(last_seen) <= OFFLINE_AFTER


def _alarm_attributes(state: DetectorState) -> dict[str, Any]:
    return {
        "alarm_since": _ts(state.alarm_since),
        "hushed_until": _ts(state.hushed_until),
        "event_id": state.alarm_event_id,
    }


@dataclass(frozen=True, kw_only=True)
class MelderBinarySensorDescription(BinarySensorEntityDescription):
    models: frozenset[str]
    value_fn: Callable[[DetectorState, dict[str, Any]], bool | None]
    attributes_fn: Callable[[DetectorState], dict[str, Any]] | None = None


BINARY_SENSORS = (
    MelderBinarySensorDescription(
        key="smoke",
        device_class=BinarySensorDeviceClass.SMOKE,
        models=frozenset({MODEL_SMOKE}),
        value_fn=lambda state, _: state.alarm,
        attributes_fn=_alarm_attributes,
    ),
    MelderBinarySensorDescription(
        key="carbon_monoxide",
        device_class=BinarySensorDeviceClass.CO,
        models=frozenset({MODEL_CO}),
        value_fn=lambda state, _: state.alarm,
        attributes_fn=_alarm_attributes,
    ),
    MelderBinarySensorDescription(
        key="tamper",
        device_class=BinarySensorDeviceClass.TAMPER,
        models=SUPPORTED_MODELS,
        value_fn=lambda state, _: state.tampered,
    ),
    MelderBinarySensorDescription(
        key="battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=SUPPORTED_MODELS,
        value_fn=lambda state, _: state.battery_low,
    ),
    MelderBinarySensorDescription(
        key="detection_chamber",
        translation_key="detection_chamber",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=frozenset({MODEL_SMOKE}),
        value_fn=lambda state, _: state.chamber_dirty,
    ),
    MelderBinarySensorDescription(
        key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=SUPPORTED_MODELS,
        value_fn=lambda _, status: _online(status),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetatmoMelderConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        MelderBinarySensor(coordinator, module, description)
        for module in coordinator.modules.values()
        for description in BINARY_SENSORS
        if module.type in description.models
    )


class MelderBinarySensor(NetatmoMelderEntity, BinarySensorEntity):
    entity_description: MelderBinarySensorDescription

    def __init__(
        self,
        coordinator: NetatmoMelderCoordinator,
        module: Module,
        description: MelderBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, module, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.detector, dict(self.status))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.detector)
