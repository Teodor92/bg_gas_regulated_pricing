"""Fetching and caching of the published tariff and calorific value."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_REGION,
    CONF_VAT_RATE,
    DEFAULT_VAT_RATE,
    DOMAIN,
    GCV_INDEX_URL,
    PRICE_INDEX_URL,
    REGIONS,
    STALE_AFTER_DAYS,
)
from .parser import (
    CalorificValue,
    ParseError,
    PriceTable,
    find_gcv_url,
    find_price_url,
    parse_gcv_xlsx,
    parse_price_pdf,
)

_LOGGER = logging.getLogger(__name__)

# Both sources change at most once a month, on a published schedule: the
# calorific value lands about 15 days before the month it covers, the tariff on
# or just before the 1st. Polling more often than that is not about catching
# changes, it is about how long a stale price is allowed to accumulate into
# cumulative Energy cost statistics after a month boundary. The poll itself is
# two small HTML requests; the documents are only downloaded when the period in
# their URL actually advances.
UPDATE_INTERVAL = timedelta(hours=6)
REQUEST_TIMEOUT = 60


@dataclass(frozen=True)
class GasPricingData:
    """Everything the sensors need for one month."""

    period: date
    price: PriceTable
    gcv: CalorificValue
    vat_rate: float
    stale: bool = False

    @property
    def price_eur_mwh(self) -> float:
        """End price including VAT, in EUR/MWh."""
        return self.price.total_incl_vat(self.vat_rate)

    @property
    def price_eur_kwh(self) -> float:
        """End price including VAT, in EUR/kWh."""
        return self.price_eur_mwh / 1000.0

    @property
    def price_eur_m3(self) -> float:
        """End price including VAT, in EUR/m3.

        This is the figure a Bulgarian gas meter needs: the tariff is set per
        MWh, and the transmission operator's representative calorific value for
        the month converts metered volume into that energy.
        """
        return self.price_eur_kwh * self.gcv.value


class BgGasPricingCoordinator(DataUpdateCoordinator[GasPricingData]):
    """Keeps the published tariff and calorific value up to date."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator from a config entry."""
        self.entry = entry
        self.region: str = entry.data[CONF_REGION]
        self._slug: str = REGIONS[self.region]["slug"]
        self._price_cache: tuple[str, PriceTable] | None = None
        self._gcv_cache: tuple[str, date, CalorificValue] | None = None
        self._last_good: GasPricingData | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.region}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )

    @property
    def vat_rate(self) -> float:
        """VAT percentage to apply to the published net tariff."""
        return self.entry.options.get(
            CONF_VAT_RATE, self.entry.data.get(CONF_VAT_RATE, DEFAULT_VAT_RATE)
        )

    async def _fetch(self, url: str) -> bytes:
        session = async_get_clientsession(self.hass)
        async with session.get(url, timeout=REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            return await response.read()

    async def _async_price(self) -> tuple[date, PriceTable]:
        index = (await self._fetch(PRICE_INDEX_URL)).decode("utf-8", "replace")
        period, url = await self.hass.async_add_executor_job(
            find_price_url, index, self._slug
        )
        if self._price_cache and self._price_cache[0] == url:
            return period, self._price_cache[1]

        _LOGGER.debug("Downloading tariff for %s from %s", self.region, url)
        table = await self.hass.async_add_executor_job(
            parse_price_pdf, await self._fetch(url), url
        )
        self._price_cache = (url, table)
        return period, table

    async def _async_gcv(self, month: date) -> CalorificValue:
        index = (await self._fetch(GCV_INDEX_URL)).decode("utf-8", "replace")
        url = await self.hass.async_add_executor_job(find_gcv_url, index, month)
        if (
            self._gcv_cache
            and self._gcv_cache[0] == url
            and self._gcv_cache[1] == month
        ):
            return self._gcv_cache[2]

        _LOGGER.debug("Downloading calorific values for %s from %s", month, url)
        value = await self.hass.async_add_executor_job(
            parse_gcv_xlsx, await self._fetch(url), month, url
        )
        self._gcv_cache = (url, month, value)
        return value

    async def _async_update_data(self) -> GasPricingData:
        month = dt_util.now().date().replace(day=1)
        try:
            period, price = await self._async_price()
            gcv = await self._async_gcv(month)
        except (TimeoutError, ClientError, ParseError, OSError) as err:
            # Holding the last known-good figure is deliberately preferred over
            # going unavailable: the Energy dashboard multiplies this price by
            # consumption as it accrues, so a gap produces silently uncosted
            # energy that cannot be recomputed later.
            if self._last_good is not None:
                _LOGGER.warning(
                    "Could not refresh Bulgarian gas pricing (%s); keeping the "
                    "figure published for %s",
                    err,
                    self._last_good.period,
                )
                stale = replace(self._last_good, stale=True)
                self._async_review_staleness(stale)
                return stale
            raise UpdateFailed(f"could not load Bulgarian gas pricing: {err}") from err

        data = GasPricingData(
            period=period, price=price, gcv=gcv, vat_rate=self.vat_rate
        )
        self._last_good = data
        self._async_review_staleness(data)
        return data

    def _async_review_staleness(self, data: GasPricingData) -> None:
        """Raise a repair issue if we are still costing gas at an old tariff.

        Failing loudly matters here because the alternative is invisible: the
        sensor keeps a plausible value and the Energy dashboard keeps costing
        consumption against it, so nothing looks wrong until the bill does.
        """
        today = dt_util.now().date()
        issue_id = f"stale_tariff_{self.region}"

        if data.period < today.replace(day=1) and today.day > STALE_AFTER_DAYS:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="stale_tariff",
                translation_placeholders={
                    "period": data.period.strftime("%B %Y"),
                    "url": PRICE_INDEX_URL,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
