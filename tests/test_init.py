"""End-to-end setup through the real client: token refresh and missing credentials."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.netatmo_melder.application_credentials import (
    async_get_description_placeholders,
)
from custom_components.netatmo_melder.const import API_BASE, OAUTH2_TOKEN

from .conftest import HOME_ID, load_fixture, setup_entry


def _expired(entry: MockConfigEntry) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data={**entry.data, "token": {**entry.data["token"], "expires_at": 0}},
    )


def _mock_cloud_api(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_BASE}homesdata", json=load_fixture("homesdata.json"))
    aioclient_mock.get(
        f"{API_BASE}homestatus", params={"home_id": HOME_ID}, json=load_fixture("homestatus.json")
    )
    aioclient_mock.get(
        f"{API_BASE}getevents", params={"home_id": HOME_ID}, json=load_fixture("getevents.json")
    )


@pytest.mark.usefixtures("setup_credentials")
async def test_expired_token_is_refreshed(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _expired(config_entry)
    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={
            "access_token": "mock-access-token-refreshed",
            "refresh_token": "mock-refresh-token-rotated",
            "expires_in": 10800,
        },
    )
    _mock_cloud_api(aioclient_mock)

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    # Netatmo rotates the refresh token on every refresh; HA stores the new one.
    assert entry.data["token"]["refresh_token"] == "mock-refresh-token-rotated"
    api_calls = [call for call in aioclient_mock.mock_calls if str(call[1]).startswith(API_BASE)]
    assert api_calls
    assert all(
        call[3]["Authorization"] == "Bearer mock-access-token-refreshed" for call in api_calls
    )
    assert hass.states.get("binary_sensor.smarter_rauchmelder_eg_smoke").state == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("setup_credentials")
async def test_rejected_refresh_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _expired(config_entry)
    aioclient_mock.post(OAUTH2_TOKEN, status=400, json={"error": "invalid_grant"})

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in entry.async_get_active_flows(hass, {"reauth"})
    )


async def test_missing_application_credential_retries(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("setup_credentials")
async def test_missing_webhook_id_is_generated(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = MockConfigEntry(
        domain=config_entry.domain,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data={k: v for k, v in config_entry.data.items() if k != "webhook_id"},
    )
    _mock_cloud_api(aioclient_mock)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["webhook_id"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_application_credentials_placeholders(hass: HomeAssistant) -> None:
    placeholders = await async_get_description_placeholders(hass)
    assert placeholders["redirect_url"] == "https://my.home-assistant.io/redirect/oauth"
    assert placeholders["developer_url"].startswith("https://dev.netatmo.com")
