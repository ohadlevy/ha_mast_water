"""MAST water meter (Mei Raanana and other MAST-served water corporations)."""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import MastAuthError, MastClient, MastConnectionError, MastError
from .const import (
    CONF_CARD,
    CONF_COMPANY_ID,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    DEFAULT_COMPANY_ID,
    DOMAIN,
)
from .coordinator import MastCoordinator
from .helpers import clean_token, token_expiry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type MastConfigEntry = ConfigEntry[MastCoordinator]


def _reply(status: int, **body: object) -> web.Response:
    return web.json_response({"ok": status == 200, **body}, status=status)


def _make_webhook_handler(entry: ConfigEntry):
    """Build the handler that accepts a fresh token pushed by the userscript."""

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        try:
            body = await request.json()
            token = clean_token(str(body["token"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            return _reply(400, error="bad_request")

        new_expiry = token_expiry(token)
        if new_expiry is None or new_expiry <= dt_util.utcnow():
            return _reply(400, error="invalid_or_expired_token")

        current = entry.data[CONF_TOKEN]
        current_expiry = token_expiry(current)
        if token == current or (
            current_expiry is not None
            and current_expiry > dt_util.utcnow()
            and current_expiry >= new_expiry
        ):
            return _reply(200, changed=False)  # nothing newer to store

        # Make sure the token works for *this* card before storing it, so a
        # token from another account can never replace a good one.
        client = MastClient(
            async_get_clientsession(hass),
            token,
            entry.data[CONF_CARD],
            entry.data.get(CONF_COMPANY_ID, DEFAULT_COMPANY_ID),
        )
        today = dt_util.now().date()
        try:
            await client.async_get_readings(today - timedelta(days=3), today)
        except MastAuthError:
            return _reply(401, error="token_rejected")
        except MastConnectionError:
            return _reply(502, error="cannot_reach_mast")
        except MastError:
            return _reply(400, error="token_not_valid_for_this_card")

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: token}
        )
        for flow in hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            match_context={"entry_id": entry.entry_id, "source": SOURCE_REAUTH},
        ):
            hass.config_entries.flow.async_abort(flow["flow_id"])
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
        _LOGGER.info("Stored a new MAST token (expires %s)", new_expiry.isoformat())
        return _reply(200, changed=True, expires=new_expiry.isoformat())

    return _handle


def _register_webhook(hass: HomeAssistant, entry: ConfigEntry, webhook_id: str) -> None:
    # A previous failed setup leaves the webhook registered on purpose (so a
    # pushed token can recover an expired entry); replace it on retry.
    with contextlib.suppress(ValueError, KeyError):
        webhook.async_unregister(hass, webhook_id)
    webhook.async_register(
        hass,
        DOMAIN,
        "MAST token push",
        webhook_id,
        _make_webhook_handler(entry),
        allowed_methods=["POST"],
    )


async def async_setup_entry(hass: HomeAssistant, entry: MastConfigEntry) -> bool:
    """Set up from a config entry."""
    card = entry.data[CONF_CARD]
    token = entry.data[CONF_TOKEN]

    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        webhook_id = webhook.async_generate_id()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: webhook_id}
        )
    # Register before the first refresh: if the stored token has expired the
    # refresh raises ConfigEntryAuthFailed, and the webhook must already be
    # listening so the userscript can push a new token.
    _register_webhook(hass, entry, webhook_id)

    client = MastClient(
        async_get_clientsession(hass),
        token,
        card,
        entry.data.get(CONF_COMPANY_ID, DEFAULT_COMPANY_ID),
    )
    coordinator = MastCoordinator(hass, entry, client, card, token)
    # Raises ConfigEntryAuthFailed (starts reauth) or ConfigEntryNotReady.
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MastConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and (webhook_id := entry.data.get(CONF_WEBHOOK_ID)):
        with contextlib.suppress(ValueError, KeyError):
            webhook.async_unregister(hass, webhook_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: MastConfigEntry) -> None:
    """Make sure the webhook is gone when the entry is deleted."""
    if webhook_id := entry.data.get(CONF_WEBHOOK_ID):
        with contextlib.suppress(ValueError, KeyError):
            webhook.async_unregister(hass, webhook_id)
