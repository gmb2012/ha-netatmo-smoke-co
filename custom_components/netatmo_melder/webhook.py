"""Netatmo webhook: Cloudhook lifecycle (K2, K4) and signed wake-up handler (K5).

The webhook is only a wake-up call. A request counts only with a valid X-Netatmo-secret
(hex HMAC-SHA256 of the raw body, keyed with the app's client secret); it then triggers a
refresh, and the state comes exclusively from getevents. The handler always answers 200:
Netatmo blocks the webhook for 24 h after 5 failed deliveries.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
import contextlib
import hashlib
import hmac
from http import HTTPStatus
import logging
from typing import Any

from aiohttp.hdrs import METH_POST
from aiohttp.web import Request, Response
from homeassistant.components import cloud
from homeassistant.components.webhook import (
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from homeassistant.const import CONF_WEBHOOK_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from .api import NetatmoApiError
from .const import (
    ADDWEBHOOK_RETRY_DELAY,
    CONF_CLOUDHOOK_URL,
    DOMAIN,
    SCAN_INTERVAL,
    WEBHOOK_ACTIVATION,
    WEBHOOK_REPLAY_CACHE_SIZE,
    WEBHOOK_SIGNATURE_HEADER,
)
from .coordinator import NetatmoMelderConfigEntry, NetatmoMelderCoordinator
from .parser import parse_webhook_payload

_LOGGER = logging.getLogger(__name__)


def valid_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time check of X-Netatmo-secret against HMAC-SHA256(secret, body)."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.strip().lower().encode())


class NetatmoMelderWebhook:
    """Registers the Cloudhook at Netatmo while Home Assistant Cloud is connected."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NetatmoMelderConfigEntry,
        coordinator: NetatmoMelderCoordinator,
        client_secret: str,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._client_secret = client_secret
        self._lock = asyncio.Lock()
        self._registered = False
        self._stop_listener = False
        self._unsub_retry: Callable[[], None] | None = None
        # sha256 of recently accepted bodies: a replayed signed request is ignored
        self._seen_bodies: OrderedDict[str, None] = OrderedDict()
        entry.async_on_unload(self._cancel_retry)

    @property
    def _webhook_id(self) -> str:
        return str(self._entry.data[CONF_WEBHOOK_ID])

    async def async_setup(self) -> None:
        """Register now if the cloud is connected, and follow connection changes."""
        if not cloud.async_active_subscription(self._hass):
            _LOGGER.warning(
                "No active Home Assistant Cloud subscription: no webhook, polling every %s only",
                SCAN_INTERVAL,
            )
            return
        if cloud.async_is_connected(self._hass):
            await self.async_register()
        self._entry.async_on_unload(
            cloud.async_listen_connection_change(self._hass, self._async_cloud_state_changed)
        )

    async def _async_cloud_state_changed(self, state: cloud.CloudConnectionState) -> None:
        if state is cloud.CloudConnectionState.CLOUD_CONNECTED:
            await self.async_register()
        elif state is cloud.CloudConnectionState.CLOUD_DISCONNECTED:
            await self.async_unregister()

    async def _async_cloudhook_url(self) -> str:
        """Cloudhook URL from the cloud prefs; recreated if it vanished, stored in entry.data.

        A URL stored in entry.data alone is not trusted: if the cloud no longer knows the
        hook (prefs reset, account change), Netatmo would post into the void.
        """
        try:
            url = await cloud.async_get_or_create_cloudhook(self._hass, self._webhook_id)
        except ValueError:
            # hass_nabucasa: "Hook is already enabled" (race with another create): recreate.
            with contextlib.suppress(ValueError):
                await cloud.async_delete_cloudhook(self._hass, self._webhook_id)
            url = await cloud.async_create_cloudhook(self._hass, self._webhook_id)
        if url != self._entry.data.get(CONF_CLOUDHOOK_URL):
            self._hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, CONF_CLOUDHOOK_URL: url}
            )
        return url

    def _cancel_retry(self) -> None:
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    def _schedule_retry(self) -> None:
        self._unsub_retry = async_call_later(
            self._hass, ADDWEBHOOK_RETRY_DELAY, self.async_register
        )

    async def async_register(self, _: Any = None) -> None:
        async with self._lock:
            self._cancel_retry()
            if self._registered:
                return
            try:
                url = await self._async_cloudhook_url()
            except Exception as err:
                # Besides CloudNotAvailable/ValueError, hass_nabucasa raises CloudError subclasses
                # (CloudApiError) and TimeoutError; none of them may break setup or the listener.
                _LOGGER.warning(
                    "Cloudhook not available (%s), polling only; retrying in %s",
                    type(err).__name__,
                    ADDWEBHOOK_RETRY_DELAY,
                )
                self._schedule_retry()
                return
            webhook_unregister(self._hass, self._webhook_id)
            webhook_register(
                self._hass,
                DOMAIN,
                "Netatmo Melder",
                self._webhook_id,
                self._async_handle,
                allowed_methods=[METH_POST],
            )
            try:
                await self._coordinator.api.addwebhook(url)
            except NetatmoApiError as err:
                _LOGGER.warning(
                    "addwebhook failed (%s), polling only; retrying in %s",
                    err,
                    ADDWEBHOOK_RETRY_DELAY,
                )
                self._schedule_retry()
                return
            self._registered = True
            _LOGGER.debug("Webhook registered at Netatmo")
            if not self._stop_listener:
                self._stop_listener = True
                # Drop the webhook while HA is down, so Netatmo does not count failures (K2).
                remove = self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self.async_unregister
                )
                self._entry.async_on_unload(remove)

    async def async_unregister(self, _: Any = None) -> None:
        async with self._lock:
            self._cancel_retry()
            webhook_unregister(self._hass, self._webhook_id)
            if not self._registered:
                return
            self._registered = False
            try:
                await self._coordinator.api.dropwebhook()
            except NetatmoApiError as err:
                _LOGGER.debug("dropwebhook failed: %s", err)

    async def _async_handle(
        self, hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response:
        # request.content works for aiohttp requests and for Nabu Casa's MockRequest (no read()).
        body = await request.content.read()
        if not valid_signature(
            self._client_secret, body, request.headers.get(WEBHOOK_SIGNATURE_HEADER)
        ):
            _LOGGER.debug("Ignoring webhook request with missing or invalid signature")
            return Response(status=HTTPStatus.OK)
        digest = hashlib.sha256(body).hexdigest()
        if digest in self._seen_bodies:
            _LOGGER.debug("Ignoring replayed webhook request")
            return Response(status=HTTPStatus.OK)
        self._seen_bodies[digest] = None
        if len(self._seen_bodies) > WEBHOOK_REPLAY_CACHE_SIZE:
            self._seen_bodies.popitem(last=False)
        payload = parse_webhook_payload(body)
        if payload is None:
            _LOGGER.debug("Ignoring signed webhook request without a JSON object body")
            return Response(status=HTTPStatus.OK)
        push_type = payload.get("push_type")
        if push_type == WEBHOOK_ACTIVATION:
            _LOGGER.info("Netatmo confirmed the webhook (webhook_activation)")
            return Response(status=HTTPStatus.OK)
        _LOGGER.debug("Webhook %s received, fetching events", push_type)
        # Retry for a missing event only if the body names one of our detectors.
        event_id = payload.get("event_id")
        known = payload.get("device_id") in self._coordinator.modules
        await self._coordinator.async_webhook_received(
            event_id if isinstance(event_id, str) and known else None
        )
        return Response(status=HTTPStatus.OK)
