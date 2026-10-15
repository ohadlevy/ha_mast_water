"""Config flow: paste a token, pick the card, get the webhook URL for the userscript."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .api import MastApiError, MastAuthError, MastClient, MastConnectionError
from .const import (
    CONF_CARD,
    CONF_COMPANY_ID,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    DEFAULT_COMPANY_ID,
    DOMAIN,
)
from .helpers import Card, clean_token, token_expiry


def webhook_urls(hass: HomeAssistant, webhook_id: str) -> tuple[str, str]:
    """Return (internal_url, external_url) for the token-push webhook."""
    path = webhook.async_generate_path(webhook_id)
    not_set = "(not configured in Home Assistant)"
    try:
        internal = get_url(hass, allow_external=False, allow_cloud=False) + path
    except NoURLAvailableError:
        internal = not_set
    try:
        external = get_url(hass, allow_internal=False) + path
    except NoURLAvailableError:
        external = not_set
    return internal, external


async def _validate(
    hass: HomeAssistant, card: str, token: str, company: str
) -> str | None:
    """Return an error key, or None when the credentials work."""
    expiry = token_expiry(token)
    if expiry is None:
        return "invalid_token"
    if expiry <= dt_util.utcnow():
        return "token_expired"
    client = MastClient(async_get_clientsession(hass), token, card, company)
    today = dt_util.now().date()
    try:
        readings = await client.async_get_readings(today - timedelta(days=14), today)
    except MastAuthError:
        return "invalid_auth"
    except MastConnectionError:
        return "cannot_connect"
    except MastApiError as err:
        if err.status == 404:
            return "no_data"  # the web app treats 404 as "no readings"
        # The API answers an unknown card number with a generic HTTP 500.
        return "invalid_card"
    return None if readings else "no_data"


class MastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._token = ""
        self._company = DEFAULT_COMPANY_ID
        self._cards: list[Card] = []
        self._card = ""
        self._webhook_id = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = clean_token(user_input[CONF_TOKEN])
            company = user_input[CONF_COMPANY_ID].strip()
            expiry = token_expiry(token)
            if expiry is None:
                errors["base"] = "invalid_token"
            elif expiry <= dt_util.utcnow():
                errors["base"] = "token_expired"
            else:
                client = MastClient(
                    async_get_clientsession(self.hass), token, "", company
                )
                cards: list[Card] = []
                try:
                    cards = await client.async_get_cards()
                except MastAuthError:
                    errors["base"] = "invalid_auth"
                except MastConnectionError:
                    errors["base"] = "cannot_connect"
                except MastApiError:
                    cards = []  # fall back to typing the card number by hand
                if not errors:
                    self._token, self._company, self._cards = token, company, cards
                    return await self.async_step_card()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): str,
                    vol.Required(CONF_COMPANY_ID, default=DEFAULT_COMPANY_ID): str,
                }
            ),
            errors=errors,
        )

    async def async_step_card(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None and len(self._cards) == 1:
            user_input = {CONF_CARD: self._cards[0].number}  # nothing to choose
        if user_input is not None:
            card = str(user_input[CONF_CARD]).strip()
            await self.async_set_unique_id(card)
            self._abort_if_unique_id_configured()
            if error := await _validate(self.hass, card, self._token, self._company):
                errors["base"] = error
            else:
                self._card = card
                self._webhook_id = webhook.async_generate_id()
                return await self.async_step_webhook()

        if self._cards:
            schema = vol.Schema(
                {
                    vol.Required(CONF_CARD, default=self._cards[0].number): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=c.number,
                                    label=f"{c.number} {c.address}".strip(),
                                )
                                for c in self._cards
                            ]
                        )
                    )
                }
            )
        else:
            schema = vol.Schema({vol.Required(CONF_CARD): str})
        return self.async_show_form(step_id="card", data_schema=schema, errors=errors)

    async def async_step_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="MAST water meter",
                data={
                    CONF_CARD: self._card,
                    CONF_TOKEN: self._token,
                    CONF_COMPANY_ID: self._company,
                    CONF_WEBHOOK_ID: self._webhook_id,
                },
            )
        internal, external = webhook_urls(self.hass, self._webhook_id)
        return self.async_show_form(
            step_id="webhook",
            data_schema=vol.Schema({}),
            description_placeholders={
                "internal_url": internal,
                "external_url": external,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the webhook URLs again (for setting up the userscript later)."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_abort(reason="reconfigure_successful")
        internal, external = webhook_urls(self.hass, entry.data[CONF_WEBHOOK_ID])
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({}),
            description_placeholders={
                "internal_url": internal,
                "external_url": external,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            token = clean_token(user_input[CONF_TOKEN])
            if error := await _validate(
                self.hass,
                entry.data[CONF_CARD],
                token,
                entry.data.get(CONF_COMPANY_ID, DEFAULT_COMPANY_ID),
            ):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )
