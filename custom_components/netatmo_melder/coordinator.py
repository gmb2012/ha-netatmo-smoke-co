"""Coordinator: polls homestatus + getevents, keeps detector state from events (K1, K5, K5b)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import NetatmoApiError, NetatmoAuthError, NetatmoMelderApi, find_detector_home
from .const import (
    ALARM_MAX_AGE,
    DOMAIN,
    EVENT_ALARM,
    SCAN_INTERVAL,
    STORAGE_VERSION,
    SUPPORTED_MODELS,
    WEBHOOK_EVENT_RETRIES,
    WEBHOOK_EVENT_RETRY_DELAY,
    WEBHOOK_REFRESH_MIN_INTERVAL,
    WEBHOOK_SAVE_DELAY,
)
from .parser import DetectorState, EventBatch, acknowledge, apply_events

_LOGGER = logging.getLogger(__name__)

type NetatmoMelderConfigEntry = ConfigEntry[NetatmoMelderCoordinator]


@dataclass(frozen=True, slots=True)
class Module:
    id: str
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class MelderData:
    states: Mapping[str, DetectorState]
    status: Mapping[str, Mapping[str, Any]]
    last_webhook: datetime | None


def melder_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Store of processed event ids, detector states and the last webhook time."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


class NetatmoMelderCoordinator(DataUpdateCoordinator[MelderData]):
    """homesdata once, then homestatus + getevents (home-wide) every 5 min and on webhooks."""

    def __init__(
        self, hass: HomeAssistant, entry: NetatmoMelderConfigEntry, api: NetatmoMelderApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.api = api
        self.home_id = ""
        self.homesdata: dict[str, Any] = {}
        self.modules: dict[str, Module] = {}
        self._store = melder_store(hass, entry.entry_id)
        self._states: dict[str, DetectorState] = {}
        self._processed: dict[str, int] = {}
        self._last_webhook: datetime | None = None
        # True until the first successful refresh after setup/restart (backfill, K5b).
        self._backfill = True
        self._save_pending = False
        self._last_webhook_refresh: datetime | None = None
        self._awaited_event_ids: frozenset[str] = frozenset()
        self._unsub_throttled: Callable[[], None] | None = None
        self._unsub_retry: Callable[[], None] | None = None
        self._closing = False

    async def _call[T](self, request: Awaitable[T]) -> T:
        try:
            return await request
        except NetatmoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NetatmoApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_setup(self) -> None:
        self._backfill = True
        stored = await self._store.async_load() or {}
        self._processed = dict(stored.get("processed", {}))
        self._states = {
            module_id: DetectorState.from_dict(state)
            for module_id, state in stored.get("states", {}).items()
        }
        if last_webhook := stored.get("last_webhook"):
            self._last_webhook = dt_util.parse_datetime(last_webhook)

        self.homesdata = await self._call(self.api.homesdata())
        home = find_detector_home(self.homesdata)
        if home is None:
            raise UpdateFailed("No Netatmo smoke or CO alarm found")
        self.home_id = home["id"]
        self.modules = {
            module["id"]: Module(id=module["id"], name=module.get("name", ""), type=module["type"])
            for module in home.get("modules", [])
            if module.get("type") in SUPPORTED_MODELS
        }

    async def _async_update_data(self) -> MelderData:
        modules = await self._call(self.api.homestatus(self.home_id))
        events = await self._call(self.api.getevents(self.home_id))

        batch = apply_events(self._states, events, self._processed, frozenset(self.modules))
        changed = batch.processed != self._processed
        self._states, self._processed = batch.states, batch.processed
        if changed:
            await self._async_save()
        self._fire_alarms(batch)
        self._backfill = False

        status = {
            m["id"]: m for m in modules if isinstance(m, dict) and m.get("id") in self.modules
        }
        return self._snapshot(status)

    def _fire_alarms(self, batch: EventBatch) -> None:
        now = dt_util.utcnow()
        for event in batch.alarms:
            module = self.modules[event.module_id]
            when = dt_util.utc_from_timestamp(event.time)
            if self._backfill and now - when > ALARM_MAX_AGE:
                # First refresh after setup/restart: state is set, but no push for old alarms.
                _LOGGER.info(
                    "Alarm event %s of %s from %s is older than %s: state updated, no %s event",
                    event.id,
                    module.name,
                    when.isoformat(),
                    ALARM_MAX_AGE,
                    EVENT_ALARM,
                )
                continue
            _LOGGER.warning("Alarm %s from %s at %s", event.type, module.name, when.isoformat())
            self.hass.bus.async_fire(
                EVENT_ALARM,
                {
                    "module_id": event.module_id,
                    "name": module.name,
                    "type": event.type,
                    "event_id": event.id,
                    "time": when.isoformat(),
                    # False when the alarm already ended before it was seen (late push)
                    "active": batch.states[event.module_id].alarm,
                },
            )

    def _snapshot(self, status: Mapping[str, Mapping[str, Any]]) -> MelderData:
        return MelderData(states=dict(self._states), status=status, last_webhook=self._last_webhook)

    def _store_data(self) -> dict[str, Any]:
        return {
            "processed": self._processed,
            "states": {key: state.as_dict() for key, state in self._states.items()},
            "last_webhook": self._last_webhook.isoformat() if self._last_webhook else None,
        }

    async def _async_save(self) -> None:
        self._save_pending = False
        await self._store.async_save(self._store_data())

    async def async_webhook_received(self, event_id: str | None = None) -> None:
        """A signed webhook arrived: remember when, then fetch the events (never trust the body).

        Webhook-triggered refreshes are throttled to one per WEBHOOK_REFRESH_MIN_INTERVAL; a
        webhook inside the window gets one trailing refresh at its end.
        """
        if self._closing:
            return
        now = dt_util.utcnow()
        self._last_webhook = now
        self._save_pending = True
        self._store.async_delay_save(self._store_data, WEBHOOK_SAVE_DELAY)
        if event_id:
            self._awaited_event_ids = self._awaited_event_ids | {event_id}
        if self._unsub_throttled is not None:
            return
        if self._last_webhook_refresh is not None:
            wait = self._last_webhook_refresh + WEBHOOK_REFRESH_MIN_INTERVAL - now
            if wait.total_seconds() > 0:
                self._unsub_throttled = async_call_later(
                    self.hass, wait, self._async_throttled_refresh
                )
                return
        self.config_entry.async_create_task(
            self.hass, self._async_webhook_refresh(), f"{DOMAIN} webhook refresh"
        )

    async def _async_throttled_refresh(self, _now: datetime) -> None:
        self._unsub_throttled = None
        await self._async_webhook_refresh()

    async def _async_webhook_refresh(self) -> None:
        self._last_webhook_refresh = dt_util.utcnow()
        await self.async_refresh()
        self._check_awaited_events(WEBHOOK_EVENT_RETRIES)

    async def _async_retry_awaited(self, retries_left: int, _now: datetime) -> None:
        self._unsub_retry = None
        await self.async_refresh()
        self._check_awaited_events(retries_left)

    def _check_awaited_events(self, retries_left: int) -> None:
        """Re-fetch a little later while getevents does not show the webhook's event yet."""
        if self._closing:
            return
        self._awaited_event_ids = frozenset(
            event_id for event_id in self._awaited_event_ids if event_id not in self._processed
        )
        if not self._awaited_event_ids or self._unsub_retry is not None:
            return
        if retries_left <= 0:
            _LOGGER.debug(
                "Webhook events %s not in getevents yet, the next poll will pick them up",
                sorted(self._awaited_event_ids),
            )
            self._awaited_event_ids = frozenset()
            return
        self._unsub_retry = async_call_later(
            self.hass,
            WEBHOOK_EVENT_RETRY_DELAY,
            partial(self._async_retry_awaited, retries_left - 1),
        )

    async def async_shutdown(self) -> None:
        self._closing = True
        for unsub in (self._unsub_throttled, self._unsub_retry):
            if unsub is not None:
                unsub()
        self._unsub_throttled = self._unsub_retry = None
        await super().async_shutdown()
        if self._save_pending:
            await self._async_save()

    async def async_acknowledge(self, module_id: str) -> None:
        """Reset the alarm of one detector locally (button)."""
        current = self._states.get(module_id, DetectorState())
        self._states = {**self._states, module_id: acknowledge(current)}
        await self._async_save()
        self.async_set_updated_data(self._snapshot(self.data.status))
