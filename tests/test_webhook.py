"""Tests for the webhook: HMAC check, wake-up refresh, Cloudhook and Netatmo registration."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import cloud, webhook
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.aiohttp import MockRequest
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.netatmo_melder.api import NetatmoConnectionError
from custom_components.netatmo_melder.const import (
    ADDWEBHOOK_RETRY_DELAY,
    CONF_CLOUDHOOK_URL,
    WEBHOOK_EVENT_RETRY_DELAY,
    WEBHOOK_REFRESH_MIN_INTERVAL,
    WEBHOOK_REPLAY_CACHE_SIZE,
)
from custom_components.netatmo_melder.webhook import valid_signature

from . import SIGNAL_CLOUD_CONNECTION_STATE, CloudError, CloudStub
from .conftest import (
    CLOUDHOOK_URL,
    HUSH_ID,
    SMOKE_OFF_ID,
    SMOKE_ON_ID,
    WEBHOOK_ID,
    events_without,
    load_fixture,
    setup_entry,
    sign,
    tick,
)

WEBHOOK_PATH = f"/api/webhook/{WEBHOOK_ID}"
LAST_WEBHOOK = "sensor.netatmo_melder_last_webhook_received"

pytestmark = pytest.mark.usefixtures("setup_credentials")


def _payload(index: int) -> bytes:
    return json.dumps(load_fixture("webhook_payloads.json")[index]).encode()


def _signed(body: bytes) -> dict[str, str]:
    return {"X-Netatmo-secret": sign(body)}


def _entry_with(entry: MockConfigEntry, **data: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data={**entry.data, **data},
    )


@pytest.fixture
async def registered(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    freezer: FrozenDateTimeFactory,
) -> dict[str, AsyncMock]:
    """Set up after freezing time, so later ticks do not jump over the poll interval."""
    await setup_entry(hass, config_entry)
    return mock_api


async def _post(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    body: bytes,
    headers: dict[str, str] | None = None,
) -> int:
    client = await hass_client_no_auth()
    resp = await client.post(WEBHOOK_PATH, data=body, headers=headers or {})
    await hass.async_block_till_done()
    return resp.status


# --- registration / Cloudhook -------------------------------------------------------------


async def test_setup_registers_cloudhook_at_netatmo(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    registered: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    assert cloud_connected.created == [WEBHOOK_ID]
    registered["addwebhook"].assert_awaited_once_with(CLOUDHOOK_URL)
    assert config_entry.data[CONF_CLOUDHOOK_URL] == CLOUDHOOK_URL


async def test_existing_cloudhook_is_reused(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    known = "https://hooks.example.invalid/known"
    cloud_connected.hooks[WEBHOOK_ID] = {"cloudhook_url": known}
    entry = _entry_with(config_entry, cloudhook_url=known)
    await setup_entry(hass, entry)
    assert cloud_connected.created == []
    mock_api["addwebhook"].assert_awaited_once_with(known)
    assert entry.data[CONF_CLOUDHOOK_URL] == known


async def test_stale_stored_url_is_recreated(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    """The stored URL is gone from the cloud prefs: create a new hook and store its URL."""
    entry = _entry_with(config_entry, cloudhook_url="https://hooks.example.invalid/stale")
    await setup_entry(hass, entry)
    assert cloud_connected.created == [WEBHOOK_ID]
    mock_api["addwebhook"].assert_awaited_once_with(CLOUDHOOK_URL)
    assert entry.data[CONF_CLOUDHOOK_URL] == CLOUDHOOK_URL


async def test_cloud_prefs_url_wins_over_stored_url(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    current = "https://hooks.example.invalid/current"
    cloud_connected.hooks[WEBHOOK_ID] = {"cloudhook_url": current}
    entry = _entry_with(config_entry, cloudhook_url="https://hooks.example.invalid/old")
    await setup_entry(hass, entry)
    mock_api["addwebhook"].assert_awaited_once_with(current)
    assert entry.data[CONF_CLOUDHOOK_URL] == current


async def test_create_race_value_error_recreates(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    """hass_nabucasa ValueError 'already enabled' -> delete (missing: ValueError) + create."""
    with patch(
        "homeassistant.components.cloud.async_get_or_create_cloudhook",
        AsyncMock(side_effect=ValueError("Hook is already enabled for the cloud.")),
    ):
        await setup_entry(hass, config_entry)
    assert cloud_connected.created == [WEBHOOK_ID]
    mock_api["addwebhook"].assert_awaited_once_with(CLOUDHOOK_URL)


async def test_create_race_deletes_existing_hook_first(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    cloud_connected.hooks[WEBHOOK_ID] = {"cloudhook_url": "https://hooks.example.invalid/x"}
    with patch(
        "homeassistant.components.cloud.async_get_or_create_cloudhook",
        AsyncMock(side_effect=ValueError("Hook is already enabled for the cloud.")),
    ):
        await setup_entry(hass, config_entry)
    assert cloud_connected.deleted == [WEBHOOK_ID]
    mock_api["addwebhook"].assert_awaited_once_with(CLOUDHOOK_URL)


@pytest.mark.parametrize(
    "error",
    [ValueError("Cloud is not connected"), cloud.CloudNotConnected(), cloud.CloudNotAvailable()],
)
async def test_cloudhook_errors_poll_only(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    error: Exception,
) -> None:
    with (
        patch(
            "homeassistant.components.cloud.async_get_or_create_cloudhook",
            AsyncMock(side_effect=ValueError("Hook is already enabled for the cloud.")),
        ),
        patch(
            "homeassistant.components.cloud.async_create_cloudhook", AsyncMock(side_effect=error)
        ),
    ):
        await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    mock_api["addwebhook"].assert_not_awaited()


@pytest.mark.parametrize("error", [TimeoutError(), CloudError("api error")])
async def test_unexpected_cloudhook_error_polls_and_retries(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """CloudApiError/TimeoutError must not break setup: poll, retry the registration later."""
    cloud_connected.create_error = error
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    mock_api["addwebhook"].assert_not_awaited()
    assert f"Cloudhook not available ({type(error).__name__})" in caplog.text

    calls = mock_api["getevents"].await_count
    await tick(hass, freezer, timedelta(minutes=5))  # == ADDWEBHOOK_RETRY_DELAY, still failing
    assert mock_api["getevents"].await_count == calls + 1
    mock_api["addwebhook"].assert_not_awaited()

    cloud_connected.create_error = None
    await tick(hass, freezer, ADDWEBHOOK_RETRY_DELAY)
    mock_api["addwebhook"].assert_awaited_once_with(CLOUDHOOK_URL)


async def test_unexpected_cloudhook_error_in_connection_listener(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    cloud_connected.connected = False
    await setup_entry(hass, config_entry)
    cloud_connected.connected = True
    cloud_connected.create_error = CloudError("api error")
    async_dispatcher_send(
        hass, SIGNAL_CLOUD_CONNECTION_STATE, cloud.CloudConnectionState.CLOUD_CONNECTED
    )
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    mock_api["addwebhook"].assert_not_awaited()


async def test_cloud_not_connected_raises_in_stub(hass: HomeAssistant) -> None:
    """The stand-in models core: no connection -> CloudNotConnected, missing hook -> ValueError."""
    stub = CloudStub(connected=False)
    from . import install

    install(hass, stub)
    with pytest.raises(cloud.CloudNotConnected):
        await cloud.async_get_or_create_cloudhook(hass, WEBHOOK_ID)
    with pytest.raises(cloud.CloudNotConnected):
        await cloud.async_create_cloudhook(hass, WEBHOOK_ID)
    with pytest.raises(ValueError, match="not enabled"):
        await cloud.async_delete_cloudhook(hass, WEBHOOK_ID)
    stub.connected = True
    stub.logged_in = False
    with pytest.raises(cloud.CloudNotAvailable):
        await cloud.async_get_or_create_cloudhook(hass, WEBHOOK_ID)
    with pytest.raises(cloud.CloudNotAvailable):
        await cloud.async_create_cloudhook(hass, WEBHOOK_ID)
    stub.logged_in = True
    await cloud.async_create_cloudhook(hass, WEBHOOK_ID)
    with pytest.raises(ValueError, match="already enabled"):
        await cloud.async_create_cloudhook(hass, WEBHOOK_ID)
    stub.set_up = False
    with pytest.raises(cloud.CloudNotAvailable):
        await cloud.async_delete_cloudhook(hass, WEBHOOK_ID)


async def test_cloud_not_connected_waits_for_connection(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    cloud_connected.connected = False
    await setup_entry(hass, config_entry)
    mock_api["addwebhook"].assert_not_awaited()

    async def send(state: cloud.CloudConnectionState) -> None:
        async_dispatcher_send(hass, SIGNAL_CLOUD_CONNECTION_STATE, state)
        await hass.async_block_till_done()

    cloud_connected.connected = True
    await send(cloud.CloudConnectionState.CLOUD_CONNECTED)
    mock_api["addwebhook"].assert_awaited_once_with(CLOUDHOOK_URL)
    # A second "connected" does not register twice.
    await send(cloud.CloudConnectionState.CLOUD_CONNECTED)
    mock_api["addwebhook"].assert_awaited_once()

    await send(cloud.CloudConnectionState.CLOUD_DISCONNECTED)
    mock_api["dropwebhook"].assert_awaited_once()

    await send(cloud.CloudConnectionState.CLOUD_CONNECTED)
    assert mock_api["addwebhook"].await_count == 2
    # Created once, then found again in the cloud prefs.
    assert cloud_connected.created == [WEBHOOK_ID]


async def test_no_subscription_polls_only(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    mock_api["addwebhook"].assert_not_awaited()
    assert "polling every" in caplog.text
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    mock_api["dropwebhook"].assert_not_awaited()


async def test_addwebhook_failure_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_api["addwebhook"].side_effect = [NetatmoConnectionError("down"), None]
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert "retrying in 0:05:00" in caplog.text
    assert mock_api["addwebhook"].await_count == 1

    await tick(hass, freezer, ADDWEBHOOK_RETRY_DELAY)
    assert mock_api["addwebhook"].await_count == 2
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_api["dropwebhook"].assert_awaited_once()


async def test_addwebhook_retry_cancelled_on_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_api["addwebhook"].side_effect = NetatmoConnectionError("down")
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    await tick(hass, freezer, ADDWEBHOOK_RETRY_DELAY * 2)
    assert mock_api["addwebhook"].await_count == 1
    mock_api["dropwebhook"].assert_not_awaited()


async def test_addwebhook_retry_cancelled_on_cloud_disconnect(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_api["addwebhook"].side_effect = NetatmoConnectionError("down")
    await setup_entry(hass, config_entry)
    async_dispatcher_send(
        hass, SIGNAL_CLOUD_CONNECTION_STATE, cloud.CloudConnectionState.CLOUD_DISCONNECTED
    )
    await hass.async_block_till_done()
    await tick(hass, freezer, ADDWEBHOOK_RETRY_DELAY)
    assert mock_api["addwebhook"].await_count == 1


async def test_unload_drops_webhook(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    registered["dropwebhook"].assert_awaited_once()

    # The local webhook is gone too: requests are answered 200 and ignored.
    calls = registered["getevents"].await_count
    body = _payload(1)
    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200
    assert registered["getevents"].await_count == calls


async def test_homeassistant_stop_drops_webhook(
    hass: HomeAssistant, registered: dict[str, AsyncMock]
) -> None:
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    registered["dropwebhook"].assert_awaited_once()


async def test_dropwebhook_failure_is_tolerated(
    hass: HomeAssistant, config_entry: MockConfigEntry, registered: dict[str, AsyncMock]
) -> None:
    registered["dropwebhook"].side_effect = NetatmoConnectionError("down")
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_remove_entry_deletes_cloudhook(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    registered: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    hass.config.components.add("cloud")
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert cloud_connected.deleted == [WEBHOOK_ID]


async def test_remove_entry_without_subscription_still_deletes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    """Subscription ended after the hook was created: the hook is removed anyway."""
    cloud_connected.subscription = False
    cloud_connected.hooks[WEBHOOK_ID] = {"cloudhook_url": CLOUDHOOK_URL}
    hass.config.components.add("cloud")
    await setup_entry(hass, config_entry)
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert cloud_connected.deleted == [WEBHOOK_ID]


@pytest.mark.parametrize("cloud_loaded", [True, False])
async def test_remove_entry_tolerates_missing_hook_or_cloud(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    cloud_loaded: bool,
) -> None:
    cloud_connected.subscription = False  # no webhook registered, no hook in the prefs
    if cloud_loaded:
        hass.config.components.add("cloud")
    await setup_entry(hass, config_entry)
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert cloud_connected.deleted == []
    assert hass.config_entries.async_get_entry(config_entry.entry_id) is None


async def test_remove_entry_cloud_not_set_up(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
) -> None:
    cloud_connected.subscription = False
    cloud_connected.set_up = False
    hass.config.components.add("cloud")
    await setup_entry(hass, config_entry)
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(config_entry.entry_id) is None


# --- handler ------------------------------------------------------------------------------


async def test_valid_signature_triggers_refresh_only(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    assert hass.states.get(LAST_WEBHOOK).state == "unknown"
    calls = registered["getevents"].await_count
    body = _payload(1)  # NSD-smoke sub_type 1 of the real alarm

    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200

    assert registered["getevents"].await_count == calls + 1
    assert hass.states.get(LAST_WEBHOOK).state != "unknown"
    # The fixture events end with "smoke cleared": the body (sub_type 1) did not set any state.
    assert hass.states.get("binary_sensor.smarter_rauchmelder_eg_smoke").state == "off"


async def test_cloudhook_request_is_handled(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
) -> None:
    """Nabu Casa delivers cloudhooks as MockRequest, which has no read() of its own."""
    calls = registered["getevents"].await_count
    body = _payload(1)
    request = MockRequest(
        content=body, mock_source="cloudhook", method="POST", headers=_signed(body)
    )

    resp = await webhook.async_handle_webhook(hass, WEBHOOK_ID, request)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert registered["getevents"].await_count == calls + 1
    assert hass.states.get(LAST_WEBHOOK).state != "unknown"


async def test_webhook_body_never_sets_state(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """K5: even a validly signed alarm webhook changes nothing if getevents has no alarm."""
    registered["getevents"].return_value = []
    body = json.dumps(
        {"event_type": "smoke", "device_id": "70:ee:50:00:00:01", "sub_type": 1}
    ).encode()
    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200
    assert hass.states.get("binary_sensor.smarter_rauchmelder_keller_smoke").state == "off"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Netatmo-secret": ""},
        {"X-Netatmo-secret": "0" * 64},
        {"X-Netatmo-secret": "wrong"},
        {"X-Netatmo-secret": "ünïcode"},
    ],
)
async def test_invalid_or_missing_signature_is_ignored(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    headers: dict[str, str],
) -> None:
    calls = registered["getevents"].await_count
    assert await _post(hass, hass_client_no_auth, _payload(1), headers) == 200
    assert registered["getevents"].await_count == calls
    assert hass.states.get(LAST_WEBHOOK).state == "unknown"


async def test_signature_with_other_secret_is_ignored(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    calls = registered["getevents"].await_count
    body = _payload(1)
    headers = {"X-Netatmo-secret": sign(body, "other-secret")}
    assert await _post(hass, hass_client_no_auth, body, headers) == 200
    assert registered["getevents"].await_count == calls


@pytest.mark.parametrize("body", [b"{broken json", b"", b"[1, 2, 3]", b"\xff\xfe\x00"])
async def test_broken_body_answers_200_without_refresh(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    body: bytes,
) -> None:
    calls = registered["getevents"].await_count
    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200
    assert registered["getevents"].await_count == calls


async def test_webhook_activation_is_only_logged(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = registered["getevents"].await_count
    body = _payload(0)
    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200
    assert registered["getevents"].await_count == calls
    assert "webhook_activation" in caplog.text


async def test_handler_answers_200_when_refresh_fails(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    registered["getevents"].side_effect = NetatmoConnectionError("down")
    body = _payload(3)
    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200


async def test_replayed_body_is_ignored(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    body = _payload(1)
    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200
    calls = registered["getevents"].await_count
    await tick(hass, freezer, WEBHOOK_REFRESH_MIN_INTERVAL + timedelta(seconds=1))
    polls = registered["getevents"].await_count - calls  # no poll due yet

    assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200
    assert registered["getevents"].await_count == calls + polls


async def test_replay_cache_is_bounded(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.netatmo_melder.webhook")
    client = await hass_client_no_auth()

    def body(n: int) -> bytes:
        return json.dumps({"push_type": "webhook_activation", "n": n}).encode()

    for n in range(WEBHOOK_REPLAY_CACHE_SIZE + 1):
        await client.post(WEBHOOK_PATH, data=body(n), headers=_signed(body(n)))
    await hass.async_block_till_done()
    assert "replayed" not in caplog.text

    # The newest body is still known, the oldest one fell out of the bounded cache.
    await client.post(
        WEBHOOK_PATH,
        data=body(WEBHOOK_REPLAY_CACHE_SIZE),
        headers=_signed(body(WEBHOOK_REPLAY_CACHE_SIZE)),
    )
    assert caplog.text.count("Ignoring replayed webhook request") == 1
    await client.post(WEBHOOK_PATH, data=body(0), headers=_signed(body(0)))
    assert caplog.text.count("Ignoring replayed webhook request") == 1


async def test_webhook_refreshes_are_throttled(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    calls = registered["getevents"].await_count
    for index in (1, 2, 3):  # smoke, hush, smoke-cleared within a few seconds
        body = _payload(index)
        assert await _post(hass, hass_client_no_auth, body, _signed(body)) == 200
        await tick(hass, freezer, timedelta(seconds=2))
    # Only the first one refreshed right away, the others share one trailing refresh.
    assert registered["getevents"].await_count == calls + 1

    await tick(hass, freezer, WEBHOOK_REFRESH_MIN_INTERVAL)
    assert registered["getevents"].await_count == calls + 2
    await tick(hass, freezer, WEBHOOK_REFRESH_MIN_INTERVAL)
    assert registered["getevents"].await_count == calls + 2


async def test_throttle_does_not_affect_polling(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    body = _payload(1)
    await _post(hass, hass_client_no_auth, body, _signed(body))
    calls = registered["getevents"].await_count
    await tick(hass, freezer, timedelta(minutes=5))
    assert registered["getevents"].await_count == calls + 1


async def test_missing_event_id_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """getevents lags behind the webhook: two retries ~20 s apart, then give up."""
    mock_api["getevents"].return_value = events_without(SMOKE_ON_ID, HUSH_ID, SMOKE_OFF_ID)
    await setup_entry(hass, config_entry)
    calls = mock_api["getevents"].await_count
    body = _payload(1)
    await _post(hass, hass_client_no_auth, body, _signed(body))
    assert mock_api["getevents"].await_count == calls + 1

    await tick(hass, freezer, WEBHOOK_EVENT_RETRY_DELAY)
    assert mock_api["getevents"].await_count == calls + 2
    await tick(hass, freezer, WEBHOOK_EVENT_RETRY_DELAY)
    assert mock_api["getevents"].await_count == calls + 3
    await tick(hass, freezer, WEBHOOK_EVENT_RETRY_DELAY)
    assert mock_api["getevents"].await_count == calls + 3


async def test_retry_stops_when_event_appears(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_api["getevents"].return_value = events_without(SMOKE_ON_ID, HUSH_ID, SMOKE_OFF_ID)
    await setup_entry(hass, config_entry)
    calls = mock_api["getevents"].await_count
    body = _payload(1)
    await _post(hass, hass_client_no_auth, body, _signed(body))

    mock_api["getevents"].return_value = events_without(SMOKE_OFF_ID)
    await tick(hass, freezer, WEBHOOK_EVENT_RETRY_DELAY)
    assert mock_api["getevents"].await_count == calls + 2
    assert hass.states.get("binary_sensor.smarter_rauchmelder_eg_smoke").state == "on"
    await tick(hass, freezer, WEBHOOK_EVENT_RETRY_DELAY)
    assert mock_api["getevents"].await_count == calls + 2


async def test_unknown_device_needs_no_retry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Event of a module that is no detector of this entry: one refresh, no retries."""
    await setup_entry(hass, config_entry)
    calls = mock_api["getevents"].await_count
    body = json.dumps(
        {"device_id": "70:ee:50:00:00:99", "event_id": "unknown-event", "push_type": "NSD-smoke"}
    ).encode()
    await _post(hass, hass_client_no_auth, body, _signed(body))
    await tick(hass, freezer, WEBHOOK_EVENT_RETRY_DELAY * 3)
    assert mock_api["getevents"].await_count == calls + 1


async def test_known_event_id_needs_no_retry(
    hass: HomeAssistant,
    registered: dict[str, AsyncMock],
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    calls = registered["getevents"].await_count
    body = _payload(3)  # event already in getevents
    await _post(hass, hass_client_no_auth, body, _signed(body))
    await tick(hass, freezer, WEBHOOK_EVENT_RETRY_DELAY * 3)
    assert registered["getevents"].await_count == calls + 1


async def test_pending_retry_cancelled_on_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: dict[str, AsyncMock],
    cloud_connected: CloudStub,
    hass_client_no_auth: ClientSessionGenerator,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_api["getevents"].return_value = []
    await setup_entry(hass, config_entry)
    body = _payload(1)
    await _post(hass, hass_client_no_auth, body, _signed(body))
    second = _payload(2)
    await _post(hass, hass_client_no_auth, second, _signed(second))  # throttled -> trailing
    calls = mock_api["getevents"].await_count
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await tick(hass, freezer, timedelta(minutes=1))
    assert mock_api["getevents"].await_count == calls


def test_valid_signature() -> None:
    body = _payload(2)
    assert valid_signature("test-secret", body, sign(body))
    assert valid_signature("test-secret", body, sign(body).upper())
    assert not valid_signature("test-secret", body + b" ", sign(body))
    assert not valid_signature("", body, sign(body, ""))
    assert not valid_signature("test-secret", body, None)
