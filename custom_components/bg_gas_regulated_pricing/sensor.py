"""Sensor platform for Bulgarian Natural Gas Regulated Pricing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_COMPONENTS,
    ATTR_DATA_SOURCE,
    ATTR_GCV,
    ATTR_GCV_SOURCE,
    ATTR_PERIOD,
    ATTR_PRICE_SOURCE,
    ATTR_STALE,
    ATTR_VAT_RATE,
    DOMAIN,
    REGIONS,
)
from .coordinator import BgGasPricingCoordinator, GasPricingData

# Volume unit is written out rather than taken from homeassistant.const so the
# string matches what Bulgarian gas meters and Overgas invoices use.
VOLUME_CUBIC_METERS = "m³"
ENERGY_KILO_WATT_HOUR = "kWh"
ENERGY_MEGA_WATT_HOUR = "MWh"


@dataclass(frozen=True, kw_only=True)
class BgGasSensorDescription(SensorEntityDescription):
    """Describes a sensor, including how to derive its value and unit."""

    value_fn: Callable[[GasPricingData], float]
    unit_fn: Callable[[str], str]


SENSORS: tuple[BgGasSensorDescription, ...] = (
    BgGasSensorDescription(
        key="price",
        translation_key="price",
        value_fn=lambda data: data.price_eur_m3,
        unit_fn=lambda currency: f"{currency}/{VOLUME_CUBIC_METERS}",
        suggested_display_precision=5,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BgGasSensorDescription(
        key="price_kwh",
        translation_key="price_kwh",
        value_fn=lambda data: data.price_eur_kwh,
        unit_fn=lambda currency: f"{currency}/{ENERGY_KILO_WATT_HOUR}",
        suggested_display_precision=6,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BgGasSensorDescription(
        key="price_mwh",
        translation_key="price_mwh",
        value_fn=lambda data: data.price_eur_mwh,
        unit_fn=lambda currency: f"{currency}/{ENERGY_MEGA_WATT_HOUR}",
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BgGasSensorDescription(
        key="price_mwh_excl_vat",
        translation_key="price_mwh_excl_vat",
        value_fn=lambda data: data.price.total_excl_vat,
        unit_fn=lambda currency: f"{currency}/{ENERGY_MEGA_WATT_HOUR}",
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BgGasSensorDescription(
        key="calorific_value",
        translation_key="calorific_value",
        value_fn=lambda data: data.gcv.value,
        unit_fn=lambda _currency: f"{ENERGY_KILO_WATT_HOUR}/{VOLUME_CUBIC_METERS}",
        suggested_display_precision=3,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors for a config entry."""
    coordinator: BgGasPricingCoordinator = entry.runtime_data
    currency = hass.config.currency or "EUR"
    async_add_entities(
        BgGasPriceSensor(coordinator, description, currency)
        for description in SENSORS
    )


class BgGasPriceSensor(CoordinatorEntity[BgGasPricingCoordinator], SensorEntity):
    """A single published figure derived from the regulated tariff."""

    _attr_has_entity_name = True
    entity_description: BgGasSensorDescription

    def __init__(
        self,
        coordinator: BgGasPricingCoordinator,
        description: BgGasSensorDescription,
        currency: str,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_native_unit_of_measurement = description.unit_fn(currency)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Overgas Mrezhi AD",
            model=REGIONS[coordinator.region]["area"],
            name=coordinator.entry.title,
            configuration_url="https://www.overgas.bg/za-overgaz/produkti-i-uslugi/tseni-na-prirodniya-gaz/",
        )

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if (data := self.coordinator.data) is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the tariff breakdown and where each figure came from."""
        if (data := self.coordinator.data) is None:
            return None
        attributes: dict[str, Any] = {
            ATTR_PERIOD: data.period.isoformat(),
            ATTR_STALE: data.stale,
            ATTR_DATA_SOURCE: data.source,
            ATTR_VAT_RATE: data.vat_rate,
            ATTR_GCV: data.gcv.value,
            ATTR_PRICE_SOURCE: data.price.source_url,
            ATTR_GCV_SOURCE: data.gcv.source_url,
        }
        if self.entity_description.key == "price":
            attributes[ATTR_COMPONENTS] = dict(data.price.components)
        return attributes
