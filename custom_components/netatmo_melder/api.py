"""Minimal Netatmo client for the five endpoints this integration needs (no pyatmo, K7).

Authentication is injected as an async callable returning a valid access token, so the same
client works with the config entry's OAuth2Session (token refresh) and inside the config flow
(fresh token, no entry yet).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

import aiohttp
from homeassistant import exceptions as ha_exceptions

from .const import API_BASE, REQUEST_TIMEOUT, SUPPORTED_MODELS

_AUTH_STATUSES = frozenset({HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})
# Netatmo answers a rate limit ("user usage reached") with 403 + error code 26: not an auth issue.
_ERROR_USAGE_LIMIT = 26


def _ha_errors(*names: str) -> tuple[type[BaseException], ...]:
    """Exception classes of newer Home Assistant versions; empty on versions without them."""
    return tuple(cls for name in names if (cls := getattr(ha_exceptions, name, None)))


# Since HA 2026.x OAuth2Session.async_ensure_token_valid raises these typed errors (both are
# ClientResponseError/ClientError subclasses); older versions raise plain aiohttp errors.
_TOKEN_REAUTH_ERRORS = _ha_errors("OAuth2TokenRequestReauthError")
_TOKEN_TRANSIENT_ERRORS = _ha_errors(
    "OAuth2TokenRequestTransientError", "OAuth2TokenRequestConnectionError"
)


async def _error_code(resp: aiohttp.ClientResponse) -> int | None:
    try:
        data = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return error.get("code") if isinstance(error, dict) else None


class NetatmoApiError(Exception):
    """Base class for all client errors."""


class NetatmoAuthError(NetatmoApiError):
    """Token rejected or refresh failed (HTTP 401/403): reauthentication needed."""


class NetatmoConnectionError(NetatmoApiError):
    """Cloud unreachable, rate limited (429), server error or unexpected answer."""


class NetatmoMelderApi:
    """homesdata / homestatus / getevents / addwebhook / dropwebhook."""

    def __init__(
        self, session: aiohttp.ClientSession, get_token: Callable[[], Awaitable[str]]
    ) -> None:
        self._session = session
        self._get_token = get_token

    async def _token(self) -> str:
        try:
            return await self._get_token()
        except _TOKEN_REAUTH_ERRORS as err:
            raise NetatmoAuthError(f"Token refresh rejected: {type(err).__name__}") from err
        except _TOKEN_TRANSIENT_ERRORS as err:
            raise NetatmoConnectionError(f"Token refresh failed: {type(err).__name__}") from err
        except aiohttp.ClientResponseError as err:
            # Token refresh answered 400/401/403: the refresh token is no longer valid.
            if err.status in _AUTH_STATUSES:
                raise NetatmoAuthError(f"Token refresh rejected ({err.status})") from err
            raise NetatmoConnectionError(f"Token refresh failed ({err.status})") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NetatmoConnectionError(f"Token refresh failed: {type(err).__name__}") from err

    async def _request(
        self, method: str, endpoint: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        kwargs: dict[str, Any] = {"params": params} if method == "GET" else {"data": params}
        try:
            async with self._session.request(
                method,
                f"{API_BASE}{endpoint}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                **kwargs,
            ) as resp:
                if resp.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    if await _error_code(resp) == _ERROR_USAGE_LIMIT:
                        raise NetatmoConnectionError(f"{endpoint}: usage limit reached")
                    raise NetatmoAuthError(f"{endpoint}: HTTP {resp.status}")
                if resp.status >= 400:
                    raise NetatmoConnectionError(f"{endpoint}: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise NetatmoConnectionError(f"{endpoint}: {type(err).__name__}") from err
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise NetatmoConnectionError(f"{endpoint}: unexpected answer")
        return data

    async def homesdata(self) -> dict[str, Any]:
        """Homes, rooms and modules (names, types) plus the user (id)."""
        return (await self._request("GET", "homesdata")).get("body", {})

    async def homestatus(self, home_id: str) -> list[dict[str, Any]]:
        """Module status: firmware_revision, last_seen, wifi_strength (no alarm, no battery)."""
        body = (await self._request("GET", "homestatus", {"home_id": home_id})).get("body", {})
        return body.get("home", {}).get("modules", [])

    async def getevents(self, home_id: str) -> list[dict[str, Any]]:
        """Latest events of the whole home (not filtered per detector, Phase 0)."""
        body = (await self._request("GET", "getevents", {"home_id": home_id})).get("body", {})
        return body.get("home", {}).get("events", [])

    async def addwebhook(self, url: str) -> None:
        await self._request("POST", "addwebhook", {"url": url})

    async def dropwebhook(self) -> None:
        await self._request("POST", "dropwebhook")


def find_detector_home(homesdata: dict[str, Any]) -> dict[str, Any] | None:
    """First home of the account that contains at least one NSD/NCO module."""
    for home in homesdata.get("homes", []):
        if any(module.get("type") in SUPPORTED_MODELS for module in home.get("modules", [])):
            return home
    return None
