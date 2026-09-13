#!/usr/bin/env python3
"""Read the published documents and emit prices.json.

Run by a scheduled workflow so that the scrape happens once, centrally, rather
than in every Home Assistant install. The output is deliberately boring: a
month-keyed record of what each operator published, with enough provenance to
check it against an invoice by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _integration import load

const, parser = load()

SCHEMA_VERSION = 1
GENERATOR = "bg-gas-scraper/1.0.0"
NOTICE = (
    "Derived from documents published by „Овергаз Мрежи“ АД and "
    "„Булгартрансгаз“ ЕАД."
)

# A month-over-month move larger than this is treated as a parse failure rather
# than as news. The tariff's volatile component has moved by a few percent a
# month; the calorific value sits in a ~1% band. These thresholds are wide
# enough to pass any real change and narrow enough to catch a table whose
# columns shifted underneath us.
MAX_TARIFF_DELTA = 0.25
MAX_GCV_DELTA = 0.05


class ScrapeError(RuntimeError):
    """A source could not be read, or produced something implausible."""


def fetch(url: str, timeout: int = 60) -> tuple[bytes, str | None]:
    """Return (body, Last-Modified) for url."""
    request = urllib.request.Request(
        url, headers={"User-Agent": const.USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Last-Modified")
    except (urllib.error.URLError, OSError) as err:
        raise ScrapeError(f"could not fetch {url}: {err}") from err


def http_date_to_iso(value: str | None) -> str | None:
    """Convert an HTTP date to ISO 8601 UTC, or None if unparseable."""
    if not value:
        return None
    try:
        stamp = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z")
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def current_month() -> date:
    """First of the current month, on the calendar the operators publish to."""
    return datetime.now(ZoneInfo(const.TIMEZONE)).date().replace(day=1)


def scrape_gcv(month: date, now: str) -> dict[str, Any] | None:
    """Collect the representative calorific value for month, if published."""
    try:
        index = fetch(const.GCV_INDEX_URL)[0].decode("utf-8", "replace")
        url = parser.find_gcv_url(index, month)
        body, modified = fetch(url)
        value = parser.parse_gcv_xlsx(body, month, url)
    except (ScrapeError, parser.ParseError) as err:
        print(f"  gcv: not available for {month:%Y-%m} ({err})")
        return None

    return {
        "value": value.value,
        "unit": "kWh/m3",
        "source_url": url,
        "source_last_modified": http_date_to_iso(modified),
        "observed_at": now,
    }


def scrape_month(month: date, now: str) -> dict[str, Any]:
    """Collect everything published for month.

    A month may legitimately be incomplete -- on the 1st the tariff may not be
    out yet, and at the gas year boundary the calorific workbook may not be
    either. A partial entry is recorded as partial. Nothing is ever borrowed
    from an adjacent month to fill a gap.
    """
    entry: dict[str, Any] = {"regions": {}}

    if (gcv := scrape_gcv(month, now)) is not None:
        entry["gcv"] = gcv

    try:
        index = fetch(const.PRICE_INDEX_URL)[0].decode("utf-8", "replace")
    except ScrapeError as err:
        print(f"  tariff: index unavailable ({err})")
        return entry

    for region, meta in const.REGIONS.items():
        try:
            period, url = parser.find_price_url(index, meta["slug"])
        except parser.ParseError as err:
            print(f"  {region}: no document ({err})")
            continue
        if period != month:
            print(f"  {region}: newest is {period:%Y-%m}, not {month:%Y-%m}")
            continue
        try:
            body, modified = fetch(url)
            table = parser.parse_price_pdf(body, url)
        except (ScrapeError, parser.ParseError) as err:
            print(f"  {region}: unreadable ({err})")
            continue

        entry["regions"][region] = {
            "total_excl_vat": table.total_excl_vat,
            "unit": "EUR/MWh",
            "components": dict(table.components),
            "source_url": url,
            "source_last_modified": http_date_to_iso(modified),
            "observed_at": now,
        }
        print(f"  {region}: {table.total_excl_vat} EUR/MWh")

    return entry


def _substance(months: dict[str, Any]) -> dict[str, Any]:
    """Strip the fields that change on every run, leaving the actual figures."""
    volatile = {"observed_at"}
    return {
        key: {
            field: (
                {k: v for k, v in value.items() if k not in volatile}
                if isinstance(value, dict) and field == "gcv"
                else {
                    region: {k: v for k, v in data.items() if k not in volatile}
                    for region, data in value.items()
                }
                if field == "regions"
                else value
            )
            for field, value in entry.items()
        }
        for key, entry in months.items()
    }


def previous_month(month: date) -> date:
    """Return the month before month."""
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


def check_deltas(months: dict[str, Any], month: date) -> list[str]:
    """Return complaints about implausible month-over-month movement.

    The point of this check is that the dangerous failure is not a crash, it is
    a plausible wrong number: a table whose columns shift can still sum
    correctly and still land inside the static range bounds. Comparing against
    last month catches what those checks cannot.
    """
    key, prior_key = f"{month:%Y-%m}", f"{previous_month(month):%Y-%m}"
    entry, prior = months.get(key, {}), months.get(prior_key)
    if not prior:
        return []

    problems: list[str] = []

    new_gcv = entry.get("gcv", {}).get("value")
    old_gcv = prior.get("gcv", {}).get("value")
    if new_gcv and old_gcv and abs(new_gcv - old_gcv) / old_gcv > MAX_GCV_DELTA:
        problems.append(
            f"calorific value moved from {old_gcv} to {new_gcv} kWh/m3 "
            f"({(new_gcv - old_gcv) / old_gcv:+.1%}), beyond the "
            f"{MAX_GCV_DELTA:.0%} threshold"
        )

    for region, data in entry.get("regions", {}).items():
        old = prior.get("regions", {}).get(region, {}).get("total_excl_vat")
        new = data["total_excl_vat"]
        if old and abs(new - old) / old > MAX_TARIFF_DELTA:
            problems.append(
                f"{region} moved from {old} to {new} EUR/MWh "
                f"({(new - old) / old:+.1%}), beyond the "
                f"{MAX_TARIFF_DELTA:.0%} threshold"
            )

    return problems


def next_month(month: date) -> date:
    """Return the month after month."""
    return (month.replace(day=28) + timedelta(days=7)).replace(day=1)


def check_coverage(months: dict[str, Any], today: date) -> list[str]:
    """Return complaints about data that should exist by now but does not.

    Two deadlines, because the two sources publish on different clocks. The
    tariff lands on or just after the 1st. The calorific value lands about 15
    days before the month it covers, so by the 20th the *next* month's value
    should already be in hand -- which is the only warning available before a
    gas year rollover, where the workbook becomes a different file entirely.
    """
    problems: list[str] = []
    month = today.replace(day=1)
    entry = months.get(f"{month:%Y-%m}", {})

    if today.day > 3:
        if "gcv" not in entry:
            problems.append(f"no calorific value for {month:%Y-%m} by day {today.day}")
        missing = set(const.REGIONS) - set(entry.get("regions", {}))
        if missing:
            problems.append(
                f"no tariff for {sorted(missing)} in {month:%Y-%m} "
                f"by day {today.day}"
            )

    if today.day > 20:
        upcoming = next_month(month)
        if "gcv" not in months.get(f"{upcoming:%Y-%m}", {}):
            problems.append(
                f"no calorific value published yet for {upcoming:%Y-%m}; it is "
                f"normally available from about the 15th of the prior month"
            )

    return problems


def main() -> int:
    """Scrape and write the data file."""
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--output", type=Path, required=True)
    args.add_argument(
        "--month", help="YYYY-MM to collect instead of the current month"
    )
    args.add_argument(
        "--verify-coverage",
        action="store_true",
        help="also fail when data that should be published by now is missing",
    )
    options = args.parse_args()

    month = (
        date.fromisoformat(f"{options.month}-01")
        if options.month
        else current_month()
    )
    now = datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )

    existing: dict[str, Any] = {}
    if options.output.exists():
        existing = json.loads(options.output.read_text(encoding="utf-8"))
    months: dict[str, Any] = dict(existing.get("months", {}))

    print(f"Collecting {month:%Y-%m}")
    entry = scrape_month(month, now)

    # The calorific value for next month is published about fifteen days ahead,
    # so collect it as soon as it appears rather than waiting for the month to
    # arrive. At the October boundary this is what puts the new gas year's
    # figure in place before the tariff needs it.
    upcoming = next_month(month)
    upcoming_gcv = scrape_gcv(upcoming, now)
    if upcoming_gcv is not None:
        ahead = dict(months.get(f"{upcoming:%Y-%m}", {"regions": {}}))
        ahead["gcv"] = upcoming_gcv
        months[f"{upcoming:%Y-%m}"] = ahead
        print(f"  {upcoming:%Y-%m} gcv: {upcoming_gcv['value']}")

    # Preserve anything previously recorded for this month that is missing now,
    # so a transient outage cannot erase a figure we already published.
    prior_entry = months.get(f"{month:%Y-%m}", {})
    if "gcv" not in entry and "gcv" in prior_entry:
        entry["gcv"] = prior_entry["gcv"]
    for region, data in prior_entry.get("regions", {}).items():
        entry["regions"].setdefault(region, data)

    months[f"{month:%Y-%m}"] = entry

    if problems := check_deltas(months, month):
        for problem in problems:
            print(f"REJECTED: {problem}", file=sys.stderr)
        return 2

    # Only rewrite when something substantive moved. Timestamps alone changing
    # would commit four times a day and bury the price history in noise -- and
    # would make file freshness look like a liveness signal when it is not.
    unchanged = bool(existing) and _substance(
        existing.get("months", {})
    ) == _substance(months)

    gaps: list[str] = []
    if options.verify_coverage:
        today = datetime.now(ZoneInfo(const.TIMEZONE)).date()
        gaps = check_coverage(months, today)
        for gap in gaps:
            print(f"MISSING: {gap}", file=sys.stderr)

    # Checked before this returns: a figure that is missing produces no change
    # by definition, so a coverage gap would otherwise be reported only on a
    # run that happened to write something else.
    if unchanged:
        print("No change.")
        return 3 if gaps else 0

    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "generator": GENERATOR,
        "notice": NOTICE,
        "months": dict(sorted(months.items(), reverse=True)),
    }
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote {options.output}")
    return 3 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
