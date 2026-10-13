"""Netatmo smoke (NSD) and CO (NCO) alarms via the Netatmo cloud API and webhook."""

from __future__ import annotations

import contextlib

from homeassistant.components import cloud
from homeassistant.components.webhook import async_generate_id
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    LocalOAuth2Implementation,
    OAuth2Session,
    async_get_config_entry_implementation,
)

from .api import NetatmoMelderApi
from .coordinator import NetatmoMelderConfigEntry, NetatmoMelderCoordinator, melder_store
from .webhook import NetatmoMelderWebhook

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: NetatmoMelderConfigEntry) -> bool:
    if CONF_WEBHOOK_ID not in entry.data:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: async_generate_id()}
        )
    try:
        implementation = await async_get_config_entry_implementation(hass, entry)
    except (ImplementationUnavailableError, ValueError) as err:
        raise ConfigEntryNotReady("Netatmo application credential not available") from err
    session = OAuth2Session(hass, entry, implementation)

    async def get_token() -> str:
        await session.async_ensure_token_valid()
        return str(session.token[CONF_ACCESS_TOKEN])

    api = NetatmoMelderApi(async_get_clientsession(hass), get_token)
    coordinator = NetatmoMelderCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The client secret of the user's own dev app signs the webhook (X-Netatmo-secret).
    if isinstance(implementation, LocalOAuth2Implementation) and implementation.client_secret:
        webhook = NetatmoMelderWebhook(hass, entry, coordinator, implementation.client_secret)
        entry.async_on_unload(webhook.async_unregister)
        await webhook.async_setup()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NetatmoMelderConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: NetatmoMelderConfigEntry) -> None:
    # Regardless of the subscription: a hook may remain from an earlier subscription.
    if CONF_WEBHOOK_ID in entry.data and cloud.DOMAIN in hass.config.components:
        with contextlib.suppress(cloud.CloudNotAvailable, ValueError):
            await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
    await melder_store(hass, entry.entry_id).async_remove()
