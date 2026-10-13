"""Tests for the plain aiohttp client (aioresponses, no Home Assistant involved)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import aiohttp
from aioresponses import aioresponses
import pytest
from yarl import URL

from custom_components.netatmo_melder import api as api_module
from custom_components.netatmo_melder.api import (
    NetatmoApiError,
    NetatmoAuthError,
    NetatmoConnectionError,
    NetatmoMelderApi,
    find_detector_home,
)
from custom_components.netatmo_melder.const import API_BASE

from .conftest import ACCESS_TOKEN, HOME_ID, load_fixture

HOMESDATA = f"{API_BASE}homesdata"
HOMESTATUS = f"{API_BASE}homestatus?home_id={HOME_ID}"
GETEVENTS = f"{API_BASE}getevents?home_id={HOME_ID}"


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def mock_http() -> Generator[aioresponses]:
    with aioresponses() as mocked:
        yield mocked


async def _token() -> str:
    return ACCESS_TOKEN


@pytest.fixture
def api(session: aiohttp.ClientSession) -> NetatmoMelderApi:
    return NetatmoMelderApi(session, _token)


async def test_homesdata(api: NetatmoMelderApi, mock_http: aioresponses) -> None:
    mock_http.get(HOMESDATA, payload=load_fixture("homesdata.json"))
    body = await api.homesdata()
    assert body["user"]["id"] == "000000000000000000000002"
    assert len(body["homes"][0]["modules"]) == 4
    request = mock_http.requests[("GET", URL(HOMESDATA))][0]
    assert request.kwargs["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_homestatus(api: NetatmoMelderApi, mock_http: aioresponses) -> None:
    mock_http.get(HOMESTATUS, payload=load_fixture("homestatus.json"))
    modules = await api.homestatus(HOME_ID)
    assert [module["wifi_strength"] for module in modules] == [47, 49, 62, 68]


async def test_getevents_home_wide(api: NetatmoMelderApi, mock_http: aioresponses) -> None:
    mock_http.get(GETEVENTS, payload=load_fixture("getevents.json"))
    events = await api.getevents(HOME_ID)
    assert len(events) == 5
    assert {event["module_id"] for event in events} == {
        "70:ee:50:00:00:01",
        "70:ee:50:00:00:02",
        "70:ee:50:00:00:03",
    }


async def test_getevents_without_events(api: NetatmoMelderApi, mock_http: aioresponses) -> None:
    mock_http.get(GETEVENTS, payload={"status": "ok", "body": {"home": {"id": HOME_ID}}})
    assert await api.getevents(HOME_ID) == []


async def test_addwebhook_and_dropwebhook(api: NetatmoMelderApi, mock_http: aioresponses) -> None:
    mock_http.post(f"{API_BASE}addwebhook", payload={"status": "ok"})
    mock_http.post(f"{API_BASE}dropwebhook", payload={"status": "ok"})
    await api.addwebhook("https://hooks.example.invalid/x")
    await api.dropwebhook()
    add = mock_http.requests[("POST", URL(f"{API_BASE}addwebhook"))][0]
    assert add.kwargs["data"] == {"url": "https://hooks.example.invalid/x"}
    assert ("POST", URL(f"{API_BASE}dropwebhook")) in mock_http.requests


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (401, {"error": {"code": 2, "message": "Invalid access token"}}, NetatmoAuthError),
        (403, {"error": {"code": 3, "message": "Access token expired"}}, NetatmoAuthError),
        (403, {"error": {"code": 13, "message": "Scope"}}, NetatmoAuthError),
        (403, None, NetatmoAuthError),
        (403, {"error": {"code": 26, "message": "User usage reached"}}, NetatmoConnectionError),
        (429, {"error": {"code": 26}}, NetatmoConnectionError),
        (500, None, NetatmoConnectionError),
        (503, None, NetatmoConnectionError),
        (200, {"status": "error"}, NetatmoConnectionError),
        (200, ["not", "a", "dict"], NetatmoConnectionError),
    ],
)
async def test_http_errors(
    api: NetatmoMelderApi,
    mock_http: aioresponses,
    status: int,
    payload: object,
    error: type[NetatmoApiError],
) -> None:
    if payload is None:
        mock_http.get(HOMESDATA, status=status, body="oops")
    else:
        mock_http.get(HOMESDATA, status=status, payload=payload)
    with pytest.raises(error):
        await api.homesdata()


async def test_invalid_json(api: NetatmoMelderApi, mock_http: aioresponses) -> None:
    mock_http.get(HOMESDATA, status=200, body="{not json")
    with pytest.raises(NetatmoConnectionError):
        await api.homesdata()


@pytest.mark.parametrize("exception", [TimeoutError(), aiohttp.ClientConnectionError("down")])
async def test_network_errors(
    api: NetatmoMelderApi, mock_http: aioresponses, exception: Exception
) -> None:
    mock_http.get(HOMESDATA, exception=exception)
    with pytest.raises(NetatmoConnectionError):
        await api.homesdata()


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (aiohttp.ClientResponseError(None, (), status=400), NetatmoAuthError),
        (aiohttp.ClientResponseError(None, (), status=401), NetatmoAuthError),
        (aiohttp.ClientResponseError(None, (), status=500), NetatmoConnectionError),
        (aiohttp.ClientConnectionError("down"), NetatmoConnectionError),
        (TimeoutError(), NetatmoConnectionError),
    ],
)
async def test_token_refresh_errors(
    session: aiohttp.ClientSession, exception: Exception, error: type[NetatmoApiError]
) -> None:
    async def failing_token() -> str:
        raise exception

    with pytest.raises(error):
        await NetatmoMelderApi(session, failing_token).homesdata()


def test_find_detector_home() -> None:
    homesdata = load_fixture("homesdata.json")["body"]
    assert find_detector_home(homesdata)["id"] == HOME_ID
    assert find_detector_home({"homes": [{"id": "x", "modules": [{"type": "NAMain"}]}]}) is None
    assert find_detector_home({}) is None


class _FakeReauthError(aiohttp.ClientResponseError):
    """Stands in for HA 2026.10 OAuth2TokenRequestReauthError (a ClientResponseError)."""


class _FakeTransientError(aiohttp.ClientResponseError):
    """Stands in for HA 2026.10 OAuth2TokenRequestTransientError."""


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        # The class decides, not the status (reauth also for e.g. 404, transient also for 400).
        (_FakeReauthError(None, (), status=404), NetatmoAuthError),
        (_FakeTransientError(None, (), status=400), NetatmoConnectionError),
    ],
)
async def test_typed_token_errors_of_newer_ha(
    session: aiohttp.ClientSession, exception: Exception, error: type[NetatmoApiError]
) -> None:
    async def failing_token() -> str:
        raise exception

    with (
        patch.object(api_module, "_TOKEN_REAUTH_ERRORS", (_FakeReauthError,)),
        patch.object(api_module, "_TOKEN_TRANSIENT_ERRORS", (_FakeTransientError,)),
        pytest.raises(error) as raised,
    ):
        await NetatmoMelderApi(session, failing_token).homesdata()
    assert type(exception).__name__ in str(raised.value)


def test_ha_error_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from homeassistant import exceptions as ha_exceptions

    monkeypatch.setattr(
        ha_exceptions, "OAuth2TokenRequestReauthError", _FakeReauthError, raising=False
    )
    assert api_module._ha_errors("OAuth2TokenRequestReauthError", "DoesNotExist") == (
        _FakeReauthError,
    )
    assert api_module._ha_errors("DoesNotExist") == ()


async def test_error_messages_do_not_leak_request_details(
    api: NetatmoMelderApi, mock_http: aioresponses
) -> None:
    mock_http.get(HOMESDATA, exception=aiohttp.ClientConnectionError("secret-detail"))
    with pytest.raises(NetatmoConnectionError) as raised:
        await api.homesdata()
    assert str(raised.value) == "homesdata: ClientConnectionError"
