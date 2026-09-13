"""The Bulgarian Natural Gas Regulated Pricing integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import BgGasPricingCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

type BgGasConfigEntry = ConfigEntry[BgGasPricingCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BgGasConfigEntry) -> bool:
    """Set up a supply area from a config entry."""
    coordinator = BgGasPricingCoordinator(hass, entry)
    await coordinator.async_restore()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BgGasConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: BgGasConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
