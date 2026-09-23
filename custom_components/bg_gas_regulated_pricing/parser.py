"""Pure parsing helpers.

Everything in this module is synchronous, network-free and dependency-light so
it can be unit tested against captured fixtures. The integration calls it from
an executor thread.
"""

from __future__ import annotations

import html
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from urllib.parse import urljoin

from .const import (
    BGN_PER_EUR,
    FX_TOLERANCE,
    GCV_BASE_URL,
    MAX_GCV_KWH_M3,
    MAX_PRICE_EUR_MWH,
    MIN_GCV_KWH_M3,
    MIN_PRICE_EUR_MWH,
    SUM_TOLERANCE,
)

_LOGGER = logging.getLogger(__name__)

MONTHS_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# Component labels as they appear in the Overgas tables, in column order. The
# CNG-supplied areas carry an extra "compression" column, so the number of
# columns varies by region -- the parser never assumes a fixed count.
COMPONENT_LABELS = (
    ("distribution", "Цена за разпределение"),
    ("supply", "Цена за снабдяване"),
    ("transmission_access", "Цена за пренос и достъп"),
    ("delivery", "Цена за доставка"),
    ("compression", "Цена за компресиране"),
)

_ROW_RE = re.compile(r"Битови\s*клиенти((?:\s+\d+[.,]\d+){5,9})")
_EUR_MARKER = "евро/МВтч"
_LEV_MARKER = "лева/МВтч"


class ParseError(ValueError):
    """Raised when a source document cannot be parsed or fails validation."""


@dataclass(frozen=True)
class PriceTable:
    """Household tariff for one supply area, in EUR/MWh excluding VAT."""

    total_excl_vat: float
    components: dict[str, float] = field(default_factory=dict)
    source_url: str | None = None

    def total_incl_vat(self, vat_rate: float) -> float:
        """Return the end price including VAT, in EUR/MWh."""
        return self.total_excl_vat * (1.0 + vat_rate / 100.0)


@dataclass(frozen=True)
class CalorificValue:
    """Representative gross calorific value for one month, in kWh/m3."""

    value: float
    month: date
    source_url: str | None = None


def _row_values(blob: str) -> list[float]:
    return [float(v.replace(",", ".")) for v in blob.split()]


def _validate_row(values: list[float]) -> tuple[float, list[float]]:
    """Split a tariff row into (total, components) and check they agree.

    The final column is always the end price and the preceding columns are its
    components. Verifying the sum is what makes the parser safe against Overgas
    adding or removing a column, which they do per supply area.
    """
    total, components = values[-1], values[:-1]
    if abs(sum(components) - total) > SUM_TOLERANCE:
        raise ParseError(
            f"tariff components {components} sum to {sum(components):.2f}, "
            f"which does not match the stated total {total:.2f}"
        )
    return total, components


def parse_price_pdf(data: bytes, source_url: str | None = None) -> PriceTable:
    """Extract the household tariff from an Overgas price PDF."""
    import pypdf  # imported lazily so the module stays importable without it

    try:
        reader = pypdf.PdfReader(BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as err:
        raise ParseError(f"could not read PDF: {err}") from err

    flat = re.sub(r"\s+", " ", text)
    rows = [_row_values(m.group(1)) for m in _ROW_RE.finditer(flat)]
    if not rows:
        raise ParseError("no 'Битови клиенти' tariff row found in the document")

    parsed = []
    for values in rows:
        try:
            parsed.append(_validate_row(values))
        except ParseError as err:
            _LOGGER.debug("Discarding malformed tariff row %s: %s", values, err)
    if not parsed:
        raise ParseError("found tariff rows but none had self-consistent totals")

    # Overgas publishes the same tariff twice during the euro transition: once
    # in legacy lev and once in euro. Pick the euro row, and where both are
    # present use the fixed conversion rate to confirm we picked correctly.
    parsed.sort(key=lambda item: item[0])
    total, components = parsed[0]

    if len(parsed) >= 2:
        ratio = parsed[-1][0] / total
        if abs(ratio - BGN_PER_EUR) > FX_TOLERANCE:
            raise ParseError(
                f"two tariff rows found but their ratio {ratio:.5f} is not the "
                f"fixed rate {BGN_PER_EUR}; cannot tell which row is in euro"
            )
    elif _LEV_MARKER in flat and _EUR_MARKER not in flat:
        raise ParseError("only a lev-denominated tariff was found")

    if not MIN_PRICE_EUR_MWH <= total <= MAX_PRICE_EUR_MWH:
        raise ParseError(
            f"tariff total {total:.2f} EUR/MWh is outside the plausible range "
            f"{MIN_PRICE_EUR_MWH}-{MAX_PRICE_EUR_MWH}"
        )

    named = {
        key: value
        for (key, _label), value in zip(COMPONENT_LABELS, components, strict=False)
    }
    # A region with more columns than we have labels for still parses; the
    # surplus is kept so nothing is silently dropped from the breakdown.
    for index, value in enumerate(components[len(COMPONENT_LABELS):]):
        named[f"component_{index + len(COMPONENT_LABELS) + 1}"] = value

    return PriceTable(total_excl_vat=total, components=named, source_url=source_url)


def _xlsx_rows(data: bytes) -> list[list[str]]:
    """Read the first worksheet of an xlsx as rows of strings, stdlib only."""
    with zipfile.ZipFile(BytesIO(data)) as archive:
        try:
            shared_xml = archive.read("xl/sharedStrings.xml").decode("utf-8")
        except KeyError:
            shared_xml = ""
        names = [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")]
        if not names:
            raise ParseError("workbook contains no worksheets")
        sheet_xml = archive.read(sorted(names)[0]).decode("utf-8")

    shared = [
        html.unescape(re.sub(r"<[^>]+>", "", chunk))
        for chunk in re.findall(r"<si>(.*?)</si>", shared_xml, re.S)
    ]

    rows: list[list[str]] = []
    for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", sheet_xml, re.S):
        cells: list[str] = []
        for attrs, body in re.findall(r"<c\s([^>]*)>(.*?)</c>", row_xml, re.S):
            match = re.search(r"<v>(.*?)</v>", body, re.S)
            if not match:
                continue
            value = match.group(1)
            if 't="s"' in attrs:
                try:
                    value = shared[int(value)]
                except (ValueError, IndexError):
                    continue
            cells.append(value)
        if cells:
            rows.append(cells)
    return rows


def _workbook_gas_year(rows: list[list[str]]) -> tuple[int, int] | None:
    """Read the gas year a workbook covers from its own header, e.g. 2025-2026."""
    for cells in rows:
        for cell in cells:
            if match := re.search(r"(20\d{2})\s*[-\u2013\u2014]\s*(20\d{2})", cell):
                return int(match.group(1)), int(match.group(2))
    return None


def gas_year(moment: date) -> tuple[int, int]:
    """Return the (start, end) two-digit years of the gas year containing moment.

    The Bulgarian gas year runs October to September, so September 2026 belongs
    to gas year 2025-2026 and October 2026 opens 2026-2027.
    """
    start = moment.year if moment.month >= 10 else moment.year - 1
    return start % 100, (start + 1) % 100


def parse_gcv_xlsx(
    data: bytes, month: date, source_url: str | None = None
) -> CalorificValue:
    """Extract the representative calorific value for month from the workbook."""
    wanted = MONTHS_EN[month.month - 1].lower()
    rows = _xlsx_rows(data)

    # Each workbook covers a single gas year, and month names repeat across
    # years. Without this check a lookup for a month outside the workbook's
    # range silently returns the same month from the year it does cover.
    if covered := _workbook_gas_year(rows):
        start, end = covered
        in_range = (month.year == start and month.month >= 10) or (
            month.year == end and month.month <= 9
        )
        if not in_range:
            raise ParseError(
                f"workbook covers gas year {start}-{end}, which does not "
                f"include {wanted.capitalize()} {month.year}"
            )

    for cells in rows:
        label = cells[0]
        if wanted not in label.lower():
            continue
        for cell in cells[1:]:
            try:
                value = float(cell.replace(",", "."))
            except ValueError:
                continue
            if not MIN_GCV_KWH_M3 <= value <= MAX_GCV_KWH_M3:
                raise ParseError(
                    f"calorific value {value} kWh/m3 for {wanted} is outside "
                    f"the plausible range {MIN_GCV_KWH_M3}-{MAX_GCV_KWH_M3}"
                )
            return CalorificValue(value=value, month=month, source_url=source_url)

    raise ParseError(f"no calorific value published for {wanted} {month.year}")


def find_price_url(index_html: str, slug: str) -> tuple[date, str]:
    """Find the newest PDF for slug, as (period, url).

    Links look like /wp-content/uploads/2026/09/TSENA-SAJT_red.pdf. The dated
    path is what tells us which month the document covers, so it is also the
    cheap freshness check: if the month has not advanced there is nothing to
    download.
    """
    pattern = re.compile(
        r"""["'](?P<url>(?:https?://[^"']*?)?/wp-content/uploads/"""
        r"""(?P<year>\d{4})/(?P<month>\d{2})/""" + re.escape(slug) + r"""\.pdf)["']""",
        re.IGNORECASE,
    )
    best: tuple[date, str] | None = None
    for match in pattern.finditer(index_html):
        published = date(int(match.group("year")), int(match.group("month")), 1)
        if best is None or published > best[0]:
            best = (published, match.group("url"))
    if best is None:
        raise ParseError(f"no price PDF matching '{slug}' found on the index page")
    return best[0], urljoin("https://www.overgas.bg/", best[1])


def find_gcv_url(index_html: str, moment: date) -> str:
    """Find the calorific value workbook for the gas year containing moment.

    Filenames are typed by hand and the end year is not reliable: the 2026-2027
    workbook went up as R_GCV_26_26October.xlsx. So the start year picks the
    file, an exact end year wins if both exist, and parse_gcv_xlsx checks the
    gas year in the workbook's own header before reading a value from it.
    """
    start, end = gas_year(moment)
    pattern = re.compile(
        r"""["'](?P<url>[^"']*?R_GCV_(?P<start>\d{2})_(?P<end>\d{2})[^"']*?\.xlsx)["']""",
        re.IGNORECASE,
    )
    candidates = [
        match
        for match in pattern.finditer(index_html)
        if int(match.group("start")) == start
    ]
    candidates.sort(key=lambda match: int(match.group("end")) != end)
    if candidates:
        return urljoin(GCV_BASE_URL, candidates[0].group("url"))
    raise ParseError(
        f"no calorific value workbook published for gas year {start:02d}-{end:02d}"
    )
