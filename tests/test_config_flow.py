"""Tests for the OAuth2 config flow (application credentials, unique_id, reauth)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL

from custom_components.netatmo_melder.const import API_BASE, DOMAIN, OAUTH2_AUTHORIZE, OAUTH2_TOKEN

from .conftest import CLIENT_ID, USER_ID, load_fixture

REDIRECT_URI = "https://example.com/auth/external/callback"
SETUP_ENTRY = "custom_components.netatmo_melder.async_setup_entry"
NEW_TOKEN = {
    "refresh_token": "mock-refresh-token-new",
    "access_token": "mock-access-token-new",
    "type": "Bearer",
    "expires_in": 10800,
    "scope": ["read_smokedetector", "read_carbonmonoxidedetector"],
}

pytestmark = pytest.mark.usefixtures("current_request_with_host", "setup_credentials")


async def _authorize(
    hass: HomeAssistant,
    result: dict[str, Any],
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    homesdata: dict[str, Any] | None = None,
    homesdata_status: int = 200,
) -> dict[str, Any]:
    """Follow the external OAuth step and finish the flow with mocked Netatmo answers."""
    state = config_entry_oauth2_flow._encode_jwt(
        hass, {"flow_id": result["flow_id"], "redirect_uri": REDIRECT_URI}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    url = URL(result["url"])
    assert str(url.with_query(None)) == OAUTH2_AUTHORIZE
    assert url.query["client_id"] == CLIENT_ID
    assert url.query["redirect_uri"] == REDIRECT_URI
    assert url.query["state"] == state
    assert url.query["scope"] == "read_smokedetector read_carbonmonoxidedetector"

    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200

    aioclient_mock.clear_requests()
    aioclient_mock.post(OAUTH2_TOKEN, json=NEW_TOKEN)
    aioclient_mock.get(
        f"{API_BASE}homesdata",
        json=homesdata if homesdata is not None else load_fixture("homesdata.json"),
        status=homesdata_status,
    )
    with patch(SETUP_ENTRY, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()
    return result


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_full_flow_creates_entry(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    result = await _authorize(hass, await _start(hass), hass_client_no_auth, aioclient_mock)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Netatmo Melder"
    entry = result["result"]
    assert entry.unique_id == USER_ID
    assert entry.data["auth_implementation"] == DOMAIN
    assert entry.data["token"]["access_token"] == "mock-access-token-new"
    assert entry.data["webhook_id"]
    # homesdata was called with the fresh token.
    _, _, _, headers = aioclient_mock.mock_calls[-1]
    assert headers["Authorization"] == "Bearer mock-access-token-new"


async def test_second_entry_same_account_aborts(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    config_entry.add_to_hass(hass)
    result = await _authorize(hass, await _start(hass), hass_client_no_auth, aioclient_mock)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("homesdata", "status", "reason"),
    [
        (None, 403, "invalid_auth"),
        (None, 500, "cannot_connect"),
        ({"status": "ok", "body": {"homes": []}}, 200, "cannot_connect"),
        (
            {"status": "ok", "body": {"homes": [], "user": {"id": USER_ID}}},
            200,
            "no_devices",
        ),
    ],
)
async def test_flow_aborts(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    homesdata: dict[str, Any] | None,
    status: int,
    reason: str,
) -> None:
    result = await _authorize(
        hass,
        await _start(hass),
        hass_client_no_auth,
        aioclient_mock,
        homesdata=homesdata if homesdata is not None else {"error": {"code": 2}},
        homesdata_status=status,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_reauth_updates_token(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _authorize(hass, result, hass_client_no_auth, aioclient_mock)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["token"]["access_token"] == "mock-access-token-new"
    assert config_entry.data["token"]["refresh_token"] == "mock-refresh-token-new"
    assert config_entry.data["webhook_id"] == "test-webhook-id"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_other_account_aborts(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    other = load_fixture("homesdata.json")
    other["body"]["user"]["id"] = "000000000000000000000099"
    result = await _authorize(hass, result, hass_client_no_auth, aioclient_mock, homesdata=other)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data["token"]["access_token"] == "mock-access-token"
