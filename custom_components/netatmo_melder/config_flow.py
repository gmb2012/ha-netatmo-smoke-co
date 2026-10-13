"""OAuth2 config flow via application credentials (pattern of core withings/netatmo)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.components.webhook import async_generate_id
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NetatmoApiError, NetatmoAuthError, NetatmoMelderApi, find_detector_home
from .const import DOMAIN, SCOPES

DEFAULT_TITLE = "Netatmo Melder"


class NetatmoMelderFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """One entry per Netatmo account (unique_id = Netatmo user id)."""

    DOMAIN = DOMAIN

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(__name__)

    @property
    def extra_authorize_data(self) -> dict[str, str]:
        return {"scope": " ".join(SCOPES)}

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        access_token = str(data[CONF_TOKEN][CONF_ACCESS_TOKEN])

        async def get_token() -> str:
            return access_token

        api = NetatmoMelderApi(async_get_clientsession(self.hass), get_token)
        try:
            homesdata = await api.homesdata()
        except NetatmoAuthError:
            return self.async_abort(reason="invalid_auth")
        except NetatmoApiError:
            return self.async_abort(reason="cannot_connect")

        user_id = homesdata.get("user", {}).get("id")
        if not user_id:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(str(user_id))

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)

        self._abort_if_unique_id_configured()
        if find_detector_home(homesdata) is None:
            return self.async_abort(reason="no_devices")
        return self.async_create_entry(
            title=DEFAULT_TITLE, data={**data, CONF_WEBHOOK_ID: async_generate_id()}
        )
