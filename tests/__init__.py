"""Tests for netatmo_melder.

homeassistant.components.cloud cannot be imported in the test venv (its import chain needs
turbojpeg, hassil, ...). The integration only uses a handful of cloud functions, so a small
stand-in is installed here, before any test module imports the integration. It is installed
unconditionally so the tests behave the same everywhere; production uses the real module.

The stand-in models the parts of core cloud + hass_nabucasa the integration relies on (checked
against HA 2026.2.3 / 2026.10.1 and hass_nabucasa/cloudhooks.py):
- async_get_or_create_cloudhook / async_create_cloudhook raise CloudNotConnected when the cloud
  is not connected and CloudNotAvailable when not logged in,
- creating an existing hook raises ValueError("Hook is already enabled for the cloud."),
- deleting a missing hook raises ValueError("Hook is not enabled for the cloud."),
- async_delete_cloudhook raises CloudNotAvailable when the cloud is not set up,
- creating a hook can fail with hass_nabucasa errors that are not HomeAssistantError
  (CloudError/CloudApiError) or with TimeoutError (CloudStub.create_error).
Tests drive it through the CloudStub object (see conftest.cloud fixtures).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import sys
import types
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

SIGNAL_CLOUD_CONNECTION_STATE = "CLOUD_CONNECTION_STATE"
_DATA_STUB = "cloud_stub"


class CloudNotAvailable(HomeAssistantError):
    """Stand-in for cloud.CloudNotAvailable."""


class CloudNotConnected(CloudNotAvailable):
    """Stand-in for cloud.CloudNotConnected."""


class CloudError(Exception):
    """Like hass_nabucasa.CloudError (base of CloudApiError): not a HomeAssistantError."""


class CloudConnectionState(Enum):
    CLOUD_CONNECTED = "cloud_connected"
    CLOUD_DISCONNECTED = "cloud_disconnected"


@dataclass
class CloudStub:
    """State of the fake Home Assistant Cloud for one hass instance."""

    set_up: bool = True
    logged_in: bool = True
    subscription: bool = True
    connected: bool = True
    hooks: dict[str, dict[str, Any]] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    create_error: BaseException | None = None


def _stub(hass: Any) -> CloudStub:
    return hass.data.get(_DATA_STUB) or CloudStub(
        set_up=False, logged_in=False, subscription=False, connected=False
    )


def install(hass: Any, stub: CloudStub) -> CloudStub:
    hass.data[_DATA_STUB] = stub
    return stub


def async_is_connected(hass: Any) -> bool:
    return _stub(hass).connected


def async_active_subscription(hass: Any) -> bool:
    return _stub(hass).logged_in and _stub(hass).subscription


def async_listen_connection_change(hass: Any, target: Any) -> Any:
    return async_dispatcher_connect(hass, SIGNAL_CLOUD_CONNECTION_STATE, target)


async def async_create_cloudhook(hass: Any, webhook_id: str) -> str:
    stub = _stub(hass)
    if not stub.connected:
        raise CloudNotConnected
    if not stub.logged_in:
        raise CloudNotAvailable
    if stub.create_error is not None:
        raise stub.create_error
    if webhook_id in stub.hooks:
        raise ValueError("Hook is already enabled for the cloud.")
    url = f"https://hooks.example.invalid/{webhook_id}/{len(stub.created)}"
    stub.hooks[webhook_id] = {"webhook_id": webhook_id, "cloudhook_url": url}
    stub.created.append(webhook_id)
    return url


async def async_get_or_create_cloudhook(hass: Any, webhook_id: str) -> str:
    stub = _stub(hass)
    if not stub.connected:
        raise CloudNotConnected
    if not stub.logged_in:
        raise CloudNotAvailable
    if hook := stub.hooks.get(webhook_id):
        return str(hook["cloudhook_url"])
    return await async_create_cloudhook(hass, webhook_id)


async def async_delete_cloudhook(hass: Any, webhook_id: str) -> None:
    stub = _stub(hass)
    if not stub.set_up:
        raise CloudNotAvailable
    if webhook_id not in stub.hooks:
        raise ValueError("Hook is not enabled for the cloud.")
    del stub.hooks[webhook_id]
    stub.deleted.append(webhook_id)


_module = types.ModuleType("homeassistant.components.cloud")
for _name in (
    "CloudNotAvailable",
    "CloudNotConnected",
    "CloudConnectionState",
    "SIGNAL_CLOUD_CONNECTION_STATE",
    "async_is_connected",
    "async_active_subscription",
    "async_listen_connection_change",
    "async_create_cloudhook",
    "async_get_or_create_cloudhook",
    "async_delete_cloudhook",
):
    setattr(_module, _name, globals()[_name])
_module.DOMAIN = "cloud"
sys.modules[_module.__name__] = _module
