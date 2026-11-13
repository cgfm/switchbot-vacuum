"""The SwitchBot Vacuum integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SwitchBotS10Coordinator
from .repairs import async_offer_to_hide_room_sensors, async_remove_room_sensors_issue

PLATFORMS: list[Platform] = [
    Platform.VACUUM, Platform.SENSOR, Platform.BINARY_SENSOR,
    Platform.BUTTON, Platform.SELECT, Platform.IMAGE,
]

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade config entries created by earlier versions."""
    if entry.version > 1:
        return False  # Created by a newer, incompatible version.
    if entry.minor_version < 2:
        # Room sensors are now hidden by default; offer to hide existing ones too.
        async_offer_to_hide_room_sensors(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SwitchBot Vacuum from a config entry."""
    coordinator = SwitchBotS10Coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    async def _async_stop_maps(event):
        await coordinator.async_stop_maps()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_maps))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await hass.data[DOMAIN][entry.entry_id].async_stop_maps()
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop repairs that refer to a removed entry."""
    async_remove_room_sensors_issue(hass, entry)
