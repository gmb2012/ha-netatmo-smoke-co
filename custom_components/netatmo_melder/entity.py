"""Base entities: one device per detector, plus one service device for the account."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import Module, NetatmoMelderCoordinator
from .parser import DetectorState


class NetatmoMelderEntity(CoordinatorEntity[NetatmoMelderCoordinator]):
    """Entity of one detector; unique_id = module id + key (no tokens)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NetatmoMelderCoordinator, module: Module, key: str) -> None:
        super().__init__(coordinator)
        self._module_id = module.id
        self._attr_unique_id = f"{module.id}_{key}"
        firmware = coordinator.data.status.get(module.id, {}).get("firmware_revision")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, module.id)},
            name=module.name,
            manufacturer=MANUFACTURER,
            model=module.type,
            sw_version=str(firmware) if firmware is not None else None,
        )

    @property
    def detector(self) -> DetectorState:
        return self.coordinator.data.states.get(self._module_id, DetectorState())

    @property
    def status(self) -> Mapping[str, Any]:
        return self.coordinator.data.status.get(self._module_id, {})


class NetatmoMelderAccountEntity(CoordinatorEntity[NetatmoMelderCoordinator]):
    """Entity of the integration itself (e.g. last webhook)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NetatmoMelderCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name=entry.title,
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
        )
