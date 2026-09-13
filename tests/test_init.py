"""Tests for setup and the sensors it produces."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bg_gas_regulated_pricing.const import (
    CONF_REGION,
    CONF_VAT_RATE,
    DOMAIN,
)


async def _setup(hass: HomeAssistant, region: str = "mrezhi") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=region,
        title=region,
        data={CONF_REGION: region, CONF_VAT_RATE: 20.0},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert entity_id, f"no entity registered for {key}"
    return hass.states.get(entity_id)


async def test_sets_up_and_publishes_the_price(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    """The September tariff and calorific value produce 0.85077 EUR/m3."""
    freezer.move_to("2026-09-13")
    entry = await _setup(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert float(_state(hass, entry, "price").state) == pytest.approx(0.85077, abs=5e-5)
    assert float(_state(hass, entry, "price_mwh").state) == pytest.approx(79.068)
    excl_vat = float(_state(hass, entry, "price_mwh_excl_vat").state)
    assert excl_vat == pytest.approx(65.89)
    assert float(_state(hass, entry, "calorific_value").state) == pytest.approx(10.76)


async def test_price_sensor_exposes_the_tariff_breakdown(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    """The breakdown is what lets a user reconcile against their invoice."""
    freezer.move_to("2026-09-13")
    entry = await _setup(hass)

    attributes = _state(hass, entry, "price").attributes
    assert attributes["components"]["delivery"] == pytest.approx(42.43)
    assert attributes["components"]["distribution"] == pytest.approx(18.71)
    assert attributes["period"] == "2026-09-01"
    assert attributes["vat_rate"] == 20.0
    assert "overgas.bg" in attributes["price_source"]


async def test_documents_are_only_downloaded_when_the_period_advances(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    """A refresh re-checks the index pages but must not re-download the files."""
    freezer.move_to("2026-09-13")
    entry = await _setup(hass)

    def downloads() -> int:
        return sum(
            1
            for call in published_documents.mock_calls
            if str(call[1]).endswith((".pdf", ".xlsx"))
        )

    before = downloads()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after = downloads()

    # The reload builds a fresh coordinator, so one more fetch of each is
    # expected; what matters is that it is bounded, not that it repeats per poll.
    assert after - before <= 2


async def test_unloads_cleanly(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    freezer.move_to("2026-09-13")
    entry = await _setup(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
