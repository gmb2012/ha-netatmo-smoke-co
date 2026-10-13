"""Hassfest-like check: strings.json and translations are complete and consistent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netatmo_melder.binary_sensor import BINARY_SENSORS
from custom_components.netatmo_melder.sensor import SENSORS

PACKAGE = Path(__file__).parent.parent / "custom_components" / "netatmo_melder"
FILES = ("strings.json", "translations/en.json", "translations/de.json")
# Abort reasons used by the code and by AbstractOAuth2FlowHandler.
ABORT_REASONS = {
    "already_configured",
    "authorize_url_timeout",
    "cannot_connect",
    "invalid_auth",
    "missing_configuration",
    "missing_credentials",
    "no_devices",
    "no_url_available",
    "oauth_error",
    "oauth_failed",
    "oauth_implementation_unavailable",
    "oauth_timeout",
    "oauth_unauthorized",
    "reauth_successful",
    "user_rejected_authorize",
    "wrong_account",
}


def _load(name: str) -> dict[str, Any]:
    return json.loads((PACKAGE / name).read_text(encoding="utf-8"))


def _keys(data: Any, prefix: str = "") -> set[str]:
    if not isinstance(data, dict):
        return {prefix}
    return {key for k, v in data.items() for key in _keys(v, f"{prefix}.{k}" if prefix else k)}


@pytest.mark.parametrize("name", FILES)
def test_all_translation_keys_present(name: str) -> None:
    data = _load(name)
    used = {("binary_sensor", d.translation_key) for d in BINARY_SENSORS if d.translation_key}
    used |= {("sensor", d.translation_key) for d in SENSORS if d.translation_key}
    for platform, key in used:
        assert data["entity"][platform][key]["name"], (name, platform, key)
    assert set(data["config"]["abort"]) == ABORT_REASONS
    assert data["config"]["step"]["reauth_confirm"]["description"]
    assert "{developer_url}" in data["application_credentials"]["description"]
    assert "{redirect_url}" in data["application_credentials"]["description"]


def test_same_keys_in_all_files() -> None:
    reference = _keys(_load("strings.json"))
    for name in FILES[1:]:
        assert _keys(_load(name)) == reference, name


def test_english_translation_matches_strings() -> None:
    assert _load("translations/en.json") == _load("strings.json")


def test_entity_names_from_device_class_have_no_translation_key() -> None:
    # smoke / carbon_monoxide / tamper / battery / connectivity use the device class name.
    for description in BINARY_SENSORS:
        if description.key != "detection_chamber":
            assert description.translation_key is None
            assert description.device_class is not None


@pytest.mark.usefixtures("loaded")
async def test_registered_entities_are_translated(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Every translation_key of a registered entity exists in all files (incl. button)."""
    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    keys = {(entry.domain, entry.translation_key) for entry in entries if entry.translation_key}
    assert ("button", "acknowledge_alarm") in keys
    assert ("sensor", "last_webhook") in keys
    for name in FILES:
        data = _load(name)
        for platform, key in keys:
            assert data["entity"][platform][key]["name"], (name, platform, key)
