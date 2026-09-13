"""Tests for the config flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bg_gas_regulated_pricing.const import (
    CONF_REGION,
    CONF_VAT_RATE,
    DOMAIN,
)


async def test_user_flow_creates_an_entry(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    """Choosing a supply area sets the integration up."""
    freezer.move_to("2026-09-13")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: "mrezhi", CONF_VAT_RATE: 20.0}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_REGION: "mrezhi", CONF_VAT_RATE: 20.0}


async def test_same_area_cannot_be_added_twice(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    """Each supply area is unique."""
    freezer.move_to("2026-09-13")
    MockConfigEntry(
        domain=DOMAIN, unique_id="mrezhi", data={CONF_REGION: "mrezhi"}
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: "mrezhi", CONF_VAT_RATE: 20.0}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
