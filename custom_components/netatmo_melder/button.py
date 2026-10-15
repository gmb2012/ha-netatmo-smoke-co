"""Button per detector: acknowledge the alarm locally."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NetatmoMelderConfigEntry
from .entity import NetatmoMelderEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetatmoMelderConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        AcknowledgeButton(coordinator, module, "acknowledge_alarm")
        for module in coordinator.modules.values()
    )


class AcknowledgeButton(NetatmoMelderEntity, ButtonEntity):
    _attr_translation_key = "acknowledge_alarm"

    async def async_press(self) -> None:
        await self.coordinator.async_acknowledge(self._module_id)
