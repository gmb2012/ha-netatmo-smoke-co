"""Tests for config entry diagnostics (redaction)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netatmo_melder.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    ACCESS_TOKEN,
    CLOUDHOOK_URL,
    HOME_ID,
    REFRESH_TOKEN,
    USER_ID,
    WEBHOOK_ID,
    load_fixture,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("setup_credentials")


async def test_diagnostics_redacts_secrets_and_personal_data(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: dict[str, Any],
) -> None:
    homesdata = load_fixture("homesdata.json")["body"]
    homesdata["homes"][0]["coordinates"] = [8.123, 47.456]
    mock_api["homesdata"].return_value = homesdata
    await setup_entry(hass, config_entry)

    result = await async_get_config_entry_diagnostics(hass, config_entry)
    dumped = json.dumps(result, default=str)

    assert result["entry"]["data"]["token"] == REDACTED
    assert result["entry"]["data"]["auth_implementation"] == REDACTED
    assert result["entry"]["data"]["webhook_id"] == REDACTED
    assert result["entry"]["data"]["cloudhook_url"] == REDACTED
    assert result["entry"]["unique_id"] == REDACTED
    assert result["homesdata"]["user"]["email"] == REDACTED
    assert result["homesdata"]["homes"][0]["coordinates"] == REDACTED
    assert result["homesdata"]["homes"][0]["rooms"][2]["module_ids"] == REDACTED
    for secret in (
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        HOME_ID,
        USER_ID,
        WEBHOOK_ID,
        CLOUDHOOK_URL,
        "user@example.com",
        "Testhaus",
        "47.456",
        "70:ee:50",  # module ids
        "Smarter Rauchmelder",  # device names, also inside event messages
        "Rauch abgezogen",
    ):
        assert secret not in dumped, secret

    # Module ids are replaced consistently by aliases; useful content stays.
    assert list(result["modules"]) == ["module_0", "module_1", "module_2", "module_3"]
    eg = "module_2"  # third module in homesdata
    assert result["modules"][eg] == {"type": "NSD"}
    assert result["status"][eg]["wifi_strength"] == 62
    assert result["status"][eg]["id"] == REDACTED
    assert result["states"][eg]["last_event_type"] == "smoke"
    assert result["states"][eg]["last_event_message"] == REDACTED
    assert result["last_webhook"] is None


async def test_diagnostics_alias_for_stored_unknown_module(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    hass_storage: dict[str, Any],
) -> None:
    """A detector removed from the account but still in the Store gets an alias too."""
    key = f"netatmo_melder.{config_entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"processed": {}, "states": {"70:ee:50:00:00:99": {"alarm": False}}},
    }
    await setup_entry(hass, config_entry)
    result = await async_get_config_entry_diagnostics(hass, config_entry)
    assert "module_4" in result["states"]
    assert "70:ee:50" not in json.dumps(result, default=str)
