"""Fetching and caching of the published tariff and calorific value."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
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
    CONF_ALLOW_DIRECT,
    CONF_REGION,
    CONF_VAT_RATE,
    DEFAULT_ALLOW_DIRECT,
    DEFAULT_VAT_RATE,
    DOMAIN,
    GCV_INDEX_URL,
    PRICE_INDEX_URL,
    PRICE_MODIFIED_URL,
    PUBLISHED_GRACE_DAYS,
    PUBLISHED_URL,
    REGIONS,
    SEED_FILE,
    SOURCE_CACHED,
    SOURCE_DIRECT,
    SOURCE_PUBLISHED,
    SOURCE_SEED,
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
from .published import (
    MalformedDocumentError,
    PublishedEntry,
    UnsupportedSchemaError,
    check_all,
    entry_for,
    newest_entry,
    parse_document,
    previous_month,
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
    source: str = SOURCE_PUBLISHED

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

    def as_baseline(self) -> PublishedEntry:
        """Expose this reading as a baseline for the month-over-month check."""
        return PublishedEntry(period=self.period, price=self.price, gcv=self.gcv)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for persistent storage."""
        return {
            "period": self.period.isoformat(),
            "vat_rate": self.vat_rate,
            "source": self.source,
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
            source=SOURCE_CACHED,
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

    @property
    def allow_direct(self) -> bool:
        """Whether this install may scrape the sources itself."""
        return self.entry.options.get(
            CONF_ALLOW_DIRECT,
            self.entry.data.get(CONF_ALLOW_DIRECT, DEFAULT_ALLOW_DIRECT),
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

    async def _async_seed(self) -> dict | None:
        """Load the snapshot shipped with the release, if there is one."""
        path = Path(__file__).parent / SEED_FILE
        try:
            raw = await self.hass.async_add_executor_job(path.read_bytes)
            return parse_document(raw)
        except (OSError, MalformedDocumentError, UnsupportedSchemaError) as err:
            _LOGGER.debug("No usable seed data: %s", err)
            return None

    def _build(
        self, entry: PublishedEntry, source: str, month: date
    ) -> GasPricingData:
        return GasPricingData(
            period=entry.period,
            price=entry.price,
            gcv=entry.gcv,
            vat_rate=self.vat_rate,
            stale=entry.period < month,
            source=source,
        )

    async def _async_from_published(self, month: date) -> GasPricingData | None:
        """Resolve this month from the centrally published data."""
        session = async_get_clientsession(self.hass)
        raw = await fetch(session, PUBLISHED_URL, REQUEST_TIMEOUT)
        document = await self.hass.async_add_executor_job(parse_document, raw)

        if (entry := entry_for(document, month, self.region)) is not None:
            check_all(entry, await self._async_baselines(document, entry))
            return self._build(entry, SOURCE_PUBLISHED, month)

        # The month is not covered yet. That is routine on the 1st, and at the
        # gas year boundary where the calorific workbook becomes a new file.
        # It is checked exactly as the current month is: this branch runs at the
        # start of every month, which is when a bad figure is most likely to be
        # met for the first time, and an unchecked figure here would also become
        # the baseline that everything after it is judged against.
        if (older := newest_entry(document, self.region, month)) is not None:
            check_all(older, await self._async_baselines(document, older))
            return self._build(older, SOURCE_PUBLISHED, month)
        return None

    async def _async_baselines(
        self, document: dict | None, candidate: PublishedEntry
    ) -> list[PublishedEntry]:
        """Collect every independent figure worth checking candidate against.

        A reading must agree with all of them.

        Two rules earn their keep here. Baselines are taken relative to the
        candidate's own period rather than today's, because the candidate is
        often last month's entry -- and asking the document for "the month
        before today" would then hand back the candidate itself, which agrees
        with itself perfectly. And there is always one baseline the document
        did not supply: what this install last believed, or failing that the
        snapshot shipped with the build. A file that is wrong throughout is
        internally consistent, so a check sourced only from that file cannot
        see it.
        """
        baselines: list[PublishedEntry] = []

        if self._last_good is not None:
            baselines.append(self._last_good.as_baseline())
        else:
            seed = await self._async_seed()
            # The snapshot is kept even when it covers the candidate's own
            # month: comparing them still answers "has this month's published
            # figure moved since the build shipped?", which is the question
            # that matters on a first poll.
            anchor = newest_entry(seed, self.region, candidate.period) if seed else None
            if anchor is not None:
                baselines.append(anchor)

        if document is not None:
            try:
                prior = entry_for(
                    document, previous_month(candidate.period), self.region
                )
            except MalformedDocumentError as err:
                # A bad figure in a month we are not serving must not take down
                # a good one we are.
                _LOGGER.warning("Ignoring unusable prior month: %s", err)
                prior = None
            if prior is not None and prior.period != candidate.period:
                baselines.append(prior)

        return baselines

    async def _async_from_direct(self, month: date) -> GasPricingData:
        """Read the source documents from this install."""
        period, price = await self._async_price()
        gcv = await self._async_gcv(period)
        # This path exists for the case where an operator changed their format
        # and the publisher broke, which is precisely when a locally parsed
        # figure is most likely to be wrong. Check it like any other.
        candidate = PublishedEntry(period=period, price=price, gcv=gcv)
        check_all(candidate, await self._async_baselines(None, candidate))
        return GasPricingData(
            period=period,
            price=price,
            gcv=gcv,
            vat_rate=self.vat_rate,
            stale=period < month,
            source=SOURCE_DIRECT,
        )

    async def _async_update_data(self) -> GasPricingData:
        month = current_month()
        today = dt_util.utcnow().astimezone(SOFIA).date()
        transport = (ClientError, TimeoutError, OSError)

        published: GasPricingData | None = None
        unsupported = False
        ir.async_delete_issue(
            self.hass, DOMAIN, f"unsupported_data_version_{self.region}"
        )
        try:
            published = await self._async_from_published(month)
        except UnsupportedSchemaError as err:
            # Do not route around this by scraping: the install is out of date
            # and needs updating, not a workaround that may disagree with what
            # every other install is reading.
            _LOGGER.error("Published pricing data is too new to read: %s", err)
            unsupported = True
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"unsupported_data_version_{self.region}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="unsupported_data_version",
            )
        except (MalformedDocumentError, *transport) as err:
            _LOGGER.warning("Could not read published pricing data: %s", err)

        if published is not None and not published.stale:
            return await self._async_accept(published)

        # Scraping directly is a last resort, and only past the grace window:
        # the likeliest reason a month is missing is that a source changed
        # format and the publisher broke, in which case every install running
        # the same parser would fail the same way, unseen.
        if (
            not unsupported
            and self.allow_direct
            and today.day > PUBLISHED_GRACE_DAYS
        ):
            try:
                return await self._async_accept(await self._async_from_direct(month))
            except (ParseError, *transport) as err:
                _LOGGER.warning("Direct read failed as well: %s", err)

        # Accept a stale published figure only if it is not older than what we
        # already hold. Otherwise a publisher rolled back, or a source that
        # times out on the direct path, would walk the price backwards a month
        # and overwrite the newer figure we had.
        if published is not None and (
            self._last_good is None or published.period >= self._last_good.period
        ):
            return await self._async_accept(published)

        if self._last_good is not None:
            _LOGGER.warning(
                "Keeping the figure published for %s", self._last_good.period
            )
            held = replace(self._last_good, stale=True, source=SOURCE_CACHED)
            self._async_review_staleness(held)
            return held

        seed = await self._async_seed()
        entry = newest_entry(seed, self.region, month) if seed else None
        if entry is not None:
            _LOGGER.warning("Falling back to the snapshot shipped with this build")
            data = self._build(entry, SOURCE_SEED, month)
            self._async_review_staleness(data)
            return data

        raise UpdateFailed("no Bulgarian gas pricing available from any source")

    async def _async_accept(self, data: GasPricingData) -> GasPricingData:
        """Record a resolved reading and report on its freshness."""
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
