"""Reading the centrally published price data.

Everything here re-checks what the publisher already checked. A figure is not
trusted because we produced it: the same validation catches a scraper bug, a
bad commit and a tampered file, which is worth more than any signature scheme
at this scale.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .const import (
    MAX_GCV_DELTA,
    MAX_GCV_KWH_M3,
    MAX_PRICE_EUR_MWH,
    MAX_TARIFF_DELTA,
    MIN_GCV_KWH_M3,
    MIN_PRICE_EUR_MWH,
    SUM_TOLERANCE,
    SUPPORTED_SCHEMA_VERSION,
)
from .parser import CalorificValue, PriceTable

_LOGGER = logging.getLogger(__name__)


class UnsupportedSchemaError(ValueError):
    """The document announces a major version this code cannot read."""


class MalformedDocumentError(ValueError):
    """The document could not be read, or failed validation."""


@dataclass(frozen=True)
class PublishedEntry:
    """One month's figures for one supply area."""

    period: date
    price: PriceTable
    gcv: CalorificValue


def month_key(month: date) -> str:
    """Return the key a month is stored under."""
    return f"{month:%Y-%m}"


def previous_month(month: date) -> date:
    """Return the month before month."""
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


def parse_document(raw: bytes) -> dict[str, Any]:
    """Decode and shape-check the published document."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise MalformedDocumentError(f"could not decode the document: {err}") from err

    if not isinstance(document, dict):
        raise MalformedDocumentError("document is not an object")

    version = document.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        # Never guess. A major this code does not know may have moved or
        # redefined a figure, and a wrong price is worse than no update.
        raise UnsupportedSchemaError(
            f"document announces schema version {version!r}, this build reads "
            f"{SUPPORTED_SCHEMA_VERSION}"
        )

    if not isinstance(document.get("months"), dict):
        raise MalformedDocumentError("document has no months")

    return document


def entry_for(
    document: dict[str, Any], month: date, region: str
) -> PublishedEntry | None:
    """Return the figures for month and region, or None if not covered.

    A month is only usable when both halves are present for it. Partial months
    are expected around the 1st and at the gas year boundary, and are reported
    as absent rather than filled in from a neighbouring month.
    """
    entry = document["months"].get(month_key(month))
    if not isinstance(entry, dict):
        return None

    gcv = entry.get("gcv")
    tariff = (entry.get("regions") or {}).get(region)
    if not isinstance(gcv, dict) or not isinstance(tariff, dict):
        return None

    try:
        components = dict(tariff["components"])
        total = float(tariff["total_excl_vat"])
        value = float(gcv["value"])
    except (KeyError, TypeError, ValueError) as err:
        raise MalformedDocumentError(
            f"{month_key(month)}/{region} is malformed: {err}"
        ) from err

    if not MIN_PRICE_EUR_MWH <= total <= MAX_PRICE_EUR_MWH:
        raise MalformedDocumentError(
            f"tariff {total} EUR/MWh for {region} is outside the plausible range"
        )
    if abs(sum(components.values()) - total) > SUM_TOLERANCE:
        raise MalformedDocumentError(
            f"components for {region} sum to {sum(components.values()):.2f}, "
            f"not the stated {total:.2f}"
        )
    if not MIN_GCV_KWH_M3 <= value <= MAX_GCV_KWH_M3:
        raise MalformedDocumentError(
            f"calorific value {value} kWh/m3 is outside the plausible range"
        )

    return PublishedEntry(
        period=month,
        price=PriceTable(
            total_excl_vat=total,
            components=components,
            source_url=tariff.get("source_url"),
        ),
        gcv=CalorificValue(
            value=value, month=month, source_url=gcv.get("source_url")
        ),
    )


def check_against(candidate: PublishedEntry, baseline: PublishedEntry | None) -> None:
    """Reject a figure that moved implausibly since baseline.

    The dangerous failure is not a crash but a plausible wrong number: a table
    whose columns shifted can still sum correctly and still sit inside the
    static range bounds. Only a comparison with what came before catches it.

    This is not proof against a slow drift within the threshold each month --
    nothing at this layer is. It catches the abrupt, which is what a parser
    regression and a bad commit both look like.
    """
    if baseline is None:
        return

    old, new = baseline.price.total_excl_vat, candidate.price.total_excl_vat
    if old and abs(new - old) / old > MAX_TARIFF_DELTA:
        raise MalformedDocumentError(
            f"tariff moved from {old} to {new} EUR/MWh "
            f"({(new - old) / old:+.1%}), beyond the "
            f"{MAX_TARIFF_DELTA:.0%} threshold"
        )

    old_gcv, new_gcv = baseline.gcv.value, candidate.gcv.value
    if old_gcv and abs(new_gcv - old_gcv) / old_gcv > MAX_GCV_DELTA:
        raise MalformedDocumentError(
            f"calorific value moved from {old_gcv} to {new_gcv} kWh/m3 "
            f"({(new_gcv - old_gcv) / old_gcv:+.1%}), beyond the "
            f"{MAX_GCV_DELTA:.0%} threshold"
        )


def check_all(
    candidate: PublishedEntry, baselines: list[PublishedEntry]
) -> None:
    """Reject a figure that disagrees with any independent baseline."""
    for baseline in baselines:
        check_against(candidate, baseline)


def newest_entry(
    document: dict[str, Any], region: str, not_after: date
) -> PublishedEntry | None:
    """Return the most recent usable entry at or before not_after."""
    candidates = sorted(
        (key for key in document["months"] if key <= month_key(not_after)),
        reverse=True,
    )
    for key in candidates:
        try:
            year, month = (int(part) for part in key.split("-"))
        except ValueError:
            continue
        try:
            if (entry := entry_for(document, date(year, month, 1), region)) is not None:
                return entry
        except MalformedDocumentError as err:
            _LOGGER.warning("Skipping unusable published entry %s: %s", key, err)
    return None
