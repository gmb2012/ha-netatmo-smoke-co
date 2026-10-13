"""Shared fixtures. Tests run offline: pytest-socket blocks real network access."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import copy
from datetime import timedelta
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.netatmo_melder.const import DOMAIN, SCAN_INTERVAL

from . import CloudStub, install as install_cloud

FIXTURES = Path(__file__).parent / "fixtures"

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-secret"
ACCESS_TOKEN = "mock-access-token"
REFRESH_TOKEN = "mock-refresh-token"
USER_ID = "000000000000000000000002"
HOME_ID = "000000000000000000000001"
WEBHOOK_ID = "test-webhook-id"
CLOUDHOOK_URL = f"https://hooks.example.invalid/{WEBHOOK_ID}/0"  # first hook of the stub

KELLER = "70:ee:50:00:00:01"
OG = "70:ee:50:00:00:02"
EG = "70:ee:50:00:00:03"
NCO = "70:ee:50:00:00:04"

# Real alarm of 13.10.2026 (kitchen, EG): smoke 1 + hush 1 at the same second, smoke 0 later.
SMOKE_ON_ID = "6ace559e8c26b00f820f1678"
HUSH_ID = "6ace559e8c26b00f820f1677"
SMOKE_OFF_ID = "6ace56db8c26b00f820f1693"
SMOKE_ON_TIME = 1791907229  # 2026-10-13T16:00:29Z


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def sign(body: bytes, secret: str = CLIENT_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def fixture_events() -> list[dict[str, Any]]:
    return copy.deepcopy(load_fixture("getevents.json")["body"]["home"]["events"])


def events_without(*event_ids: str) -> list[dict[str, Any]]:
    return [event for event in fixture_events() if event["id"] not in event_ids]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/netatmo_melder."""


@pytest.fixture
async def setup_credentials(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential(CLIENT_ID, CLIENT_SECRET), DOMAIN
    )


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Netatmo Melder",
        unique_id=USER_ID,
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "expires_in": 10800,
                "expires_at": 4_000_000_000,
                "scope": ["read_smokedetector", "read_carbonmonoxidedetector"],
            },
            "webhook_id": WEBHOOK_ID,
        },
    )


@pytest.fixture
async def mock_api(hass: HomeAssistant) -> AsyncGenerator[dict[str, AsyncMock]]:
    """Replace the network calls of NetatmoMelderApi with the Phase-0 fixtures.

    Loaded entries are unloaded while the mocks are still active: otherwise stopping hass
    at teardown would call the real dropwebhook.
    """
    mocks = {
        "homesdata": AsyncMock(return_value=load_fixture("homesdata.json")["body"]),
        "homestatus": AsyncMock(
            return_value=load_fixture("homestatus.json")["body"]["home"]["modules"]
        ),
        "getevents": AsyncMock(return_value=fixture_events()),
        "addwebhook": AsyncMock(return_value=None),
        "dropwebhook": AsyncMock(return_value=None),
    }
    with patch.multiple("custom_components.netatmo_melder.api.NetatmoMelderApi", **mocks):
        yield mocks
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
def cloud_connected(hass: HomeAssistant) -> CloudStub:
    """Active Nabu Casa subscription, cloud connected (stand-in from tests/__init__.py)."""
    return install_cloud(hass, CloudStub())


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta = SCAN_INTERVAL
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
async def loaded(
    hass: HomeAssistant,
    setup_credentials: None,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
) -> AsyncGenerator[dict[str, AsyncMock]]:
    await setup_entry(hass, config_entry)
    yield mock_api
