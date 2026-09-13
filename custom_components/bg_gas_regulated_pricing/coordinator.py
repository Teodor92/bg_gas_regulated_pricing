"""Fetching and caching of the published tariff and calorific value."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_REGION,
    CONF_VAT_RATE,
    DEFAULT_VAT_RATE,
    DOMAIN,
    GCV_INDEX_URL,
    PRICE_INDEX_URL,
    PRICE_MODIFIED_URL,
    REGIONS,
    STALE_AFTER_DAYS,
    STORAGE_KEY,
    STORAGE_VERSION,
    TIMEZONE,
)
from .fetcher import fetch, fetch_text, page_modified
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
# cumulative Energy cost statistics after a month boundary. A poll that finds
# nothing new costs 71 bytes, so the interval is set by that tolerance alone.
UPDATE_INTERVAL = timedelta(hours=6)
REQUEST_TIMEOUT = 60

SOFIA = ZoneInfo(TIMEZONE)


def current_month() -> date:
    """Return the first of the current month, in Bulgarian local time."""
    return dt_util.utcnow().astimezone(SOFIA).date().replace(day=1)


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

    def as_dict(self) -> dict[str, Any]:
        """Serialise for persistent storage."""
        return {
            "period": self.period.isoformat(),
            "vat_rate": self.vat_rate,
            "price": {
                "total_excl_vat": self.price.total_excl_vat,
                "components": dict(self.price.components),
                "source_url": self.price.source_url,
            },
            "gcv": {
                "value": self.gcv.value,
                "month": self.gcv.month.isoformat(),
                "source_url": self.gcv.source_url,
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GasPricingData:
        """Restore from persistent storage."""
        return cls(
            period=date.fromisoformat(raw["period"]),
            price=PriceTable(
                total_excl_vat=raw["price"]["total_excl_vat"],
                components=raw["price"]["components"],
                source_url=raw["price"]["source_url"],
            ),
            gcv=CalorificValue(
                value=raw["gcv"]["value"],
                month=date.fromisoformat(raw["gcv"]["month"]),
                source_url=raw["gcv"]["source_url"],
            ),
            vat_rate=raw["vat_rate"],
            stale=True,
        )


class BgGasPricingCoordinator(DataUpdateCoordinator[GasPricingData]):
    """Keeps the published tariff and calorific value up to date."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator from a config entry."""
        self.entry = entry
        self.region: str = entry.data[CONF_REGION]
        self._slug: str = REGIONS[self.region]["slug"]
        self._price: tuple[date, str, PriceTable] | None = None
        self._price_stamp: str | None = None
        self._gcv: CalorificValue | None = None
        self._last_good: GasPricingData | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )

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

    async def async_restore(self) -> None:
        """Load the last known-good figures persisted by a previous run.

        Called before the first refresh so that a restart during an outage does
        not leave the Energy dashboard recording consumption with no price at
        all -- energy recorded uncosted cannot be costed retrospectively.
        """
        try:
            if (stored := await self._store.async_load()) is not None:
                self._last_good = GasPricingData.from_dict(stored)
                _LOGGER.debug(
                    "Restored the tariff published for %s", self._last_good.period
                )
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Discarding unreadable stored pricing data: %s", err)

    async def _async_price(self) -> tuple[date, PriceTable]:
        """Return the most recently published tariff for this supply area."""
        session = async_get_clientsession(self.hass)

        # Ask the page when it last changed before asking for the page. Where
        # the hint is available and unchanged, nothing else needs fetching.
        stamp = await page_modified(session, PRICE_MODIFIED_URL)
        if stamp is not None and stamp == self._price_stamp and self._price is not None:
            period, _url, table = self._price
            return period, table

        index = await fetch_text(session, PRICE_INDEX_URL, REQUEST_TIMEOUT)
        period, url = await self.hass.async_add_executor_job(
            find_price_url, index, self._slug
        )

        if self._price is not None and self._price[1] == url:
            self._price_stamp = stamp
            return self._price[0], self._price[2]

        _LOGGER.debug("Reading the tariff for %s from %s", self.region, url)
        table = await self.hass.async_add_executor_job(
            parse_price_pdf, await fetch(session, url, REQUEST_TIMEOUT), url
        )
        self._price = (period, url, table)
        self._price_stamp = stamp
        return period, table

    async def _async_gcv(self, month: date) -> CalorificValue:
        """Return the representative calorific value for month.

        Keyed to the month the tariff is for, never to today. A price is only
        meaningful when both halves describe the same month, and pairing a
        previous month's tariff with the current month's calorific value
        produces a number that passes every other check while being wrong.
        """
        if self._gcv is not None and self._gcv.month == month:
            return self._gcv

        session = async_get_clientsession(self.hass)
        index = await fetch_text(session, GCV_INDEX_URL, REQUEST_TIMEOUT)
        url = await self.hass.async_add_executor_job(find_gcv_url, index, month)

        _LOGGER.debug("Reading calorific values for %s from %s", month, url)
        value = await self.hass.async_add_executor_job(
            parse_gcv_xlsx, await fetch(session, url, REQUEST_TIMEOUT), month, url
        )
        self._gcv = value
        return value

    async def _async_update_data(self) -> GasPricingData:
        try:
            period, price = await self._async_price()
            gcv = await self._async_gcv(period)
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
            period=period,
            price=price,
            gcv=gcv,
            vat_rate=self.vat_rate,
            stale=period < current_month(),
        )
        self._last_good = data
        await self._store.async_save(data.as_dict())
        self._async_review_staleness(data)
        return data

    def _async_review_staleness(self, data: GasPricingData) -> None:
        """Raise a repair issue if we are still costing gas at an old tariff.

        Failing loudly matters here because the alternative is invisible: the
        sensor keeps a plausible value and the Energy dashboard keeps costing
        consumption against it, so nothing looks wrong until the bill does.
        """
        today = dt_util.utcnow().astimezone(SOFIA).date()
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
