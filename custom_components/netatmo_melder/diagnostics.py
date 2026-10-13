"""Diagnostics: tokens, ids, e-mail, coordinates, names and webhook URL are redacted.

Module ids (MAC-like, guessable) are replaced by stable aliases module_0..n.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant

from .const import CONF_CLOUDHOOK_URL
from .coordinator import NetatmoMelderConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    "auth_implementation",
    "access_token",
    "refresh_token",
    CONF_WEBHOOK_ID,
    CONF_CLOUDHOOK_URL,
    "unique_id",
    "home_id",
    "user_id",
    "email",
    "mail",
    "coordinates",
    "latitude",
    "longitude",
    "id",
    "module_id",
    "module_ids",
    "name",
    "last_event_message",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NetatmoMelderConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data
    module_ids = dict.fromkeys([*coordinator.modules, *data.status, *data.states])
    alias = {module_id: f"module_{index}" for index, module_id in enumerate(module_ids)}
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        # "id"/"name" of home and user can be personal (account id, address as home name).
        "homesdata": async_redact_data(coordinator.homesdata, TO_REDACT),
        "modules": {
            alias[module_id]: {"type": module.type}
            for module_id, module in coordinator.modules.items()
        },
        "status": {
            alias[module_id]: async_redact_data(dict(status), TO_REDACT)
            for module_id, status in data.status.items()
        },
        "states": {
            alias[module_id]: async_redact_data(state.as_dict(), TO_REDACT)
            for module_id, state in data.states.items()
        },
        "last_webhook": data.last_webhook.isoformat() if data.last_webhook else None,
    }
