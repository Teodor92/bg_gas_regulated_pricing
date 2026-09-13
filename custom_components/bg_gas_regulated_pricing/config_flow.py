"""Config flow for Bulgarian Natural Gas Regulated Pricing."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ALLOW_DIRECT,
    CONF_REGION,
    CONF_VAT_RATE,
    DEFAULT_ALLOW_DIRECT,
    DEFAULT_VAT_RATE,
    DOMAIN,
    REGIONS,
)

VAT_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0, max=100, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="%"
    )
)


class BgGasPricingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which supply area the home is connected to."""
        if user_input is not None:
            region = user_input[CONF_REGION]
            await self.async_set_unique_id(region)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=REGIONS[region]["name"], data=user_input
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_REGION, default="mrezhi"): SelectSelector(
                    SelectSelectorConfig(
                        options=list(REGIONS),
                        translation_key="region",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_VAT_RATE, default=DEFAULT_VAT_RATE): VAT_SELECTOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return BgGasPricingOptionsFlow()


class BgGasPricingOptionsFlow(OptionsFlow):
    """Allow the VAT rate to be corrected after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        data = self.config_entry.data
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_VAT_RATE,
                    default=options.get(
                        CONF_VAT_RATE, data.get(CONF_VAT_RATE, DEFAULT_VAT_RATE)
                    ),
                ): VAT_SELECTOR,
                vol.Required(
                    CONF_ALLOW_DIRECT,
                    default=options.get(
                        CONF_ALLOW_DIRECT,
                        data.get(CONF_ALLOW_DIRECT, DEFAULT_ALLOW_DIRECT),
                    ),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
