"""MAST water meter (Mei Raanana and other MAST-served water corporations)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MastClient
from .const import (
    CONF_CARD,
    CONF_COMPANY_ID,
    CONF_TOKEN,
    CONF_USER_TOKEN,
    DEFAULT_COMPANY_ID,
)
from .coordinator import MastCoordinator

PLATFORMS = [Platform.SENSOR]

type MastConfigEntry = ConfigEntry[MastCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MastConfigEntry) -> bool:
    """Set up from a config entry."""
    card = entry.data[CONF_CARD]
    token = entry.data[CONF_TOKEN]

    client = MastClient(
        async_get_clientsession(hass),
        token,
        card,
        entry.data.get(CONF_COMPANY_ID, DEFAULT_COMPANY_ID),
        user_token=entry.data.get(CONF_USER_TOKEN, ""),
    )
    coordinator = MastCoordinator(hass, entry, client, card, token)
    # Raises ConfigEntryAuthFailed (starts reauth) or ConfigEntryNotReady.
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MastConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
