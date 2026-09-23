"""Tests for the document parsers.

The fixtures are the real documents published for September 2026, and the
expected numbers are cross-checked against an actual Overgas invoice for
August 2026 (customer meter 21 m3 -> 0.227220 MWh at 10.82 kWh/m3).
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from custom_components.bg_gas_regulated_pricing.parser import (
    ParseError,
    _validate_row,
    find_gcv_url,
    find_price_url,
    gas_year,
    parse_gcv_xlsx,
    parse_price_pdf,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(name="gcv_workbook")
def gcv_workbook_fixture() -> bytes:
    return read("gcv_25_26.xlsx")


# --- tariff parsing -------------------------------------------------------

@pytest.mark.parametrize(
    ("region", "total", "compression"),
    [
        ("mrezhi", 65.89, None),
        ("bansko_razlog", 82.40, 16.51),
        ("byala", 87.69, 21.80),
        ("karnobat", 83.42, 17.53),
    ],
)
def test_parses_every_supply_area(
    region: str, total: float, compression: float | None
) -> None:
    """Each area parses, including the three with an extra CNG column."""
    table = parse_price_pdf(read(f"price_{region}_2026-09.pdf"))

    assert table.total_excl_vat == pytest.approx(total)
    assert table.components["distribution"] == pytest.approx(18.71)
    assert table.components["supply"] == pytest.approx(3.01)
    assert table.components["transmission_access"] == pytest.approx(1.74)
    assert table.components["delivery"] == pytest.approx(42.43)

    if compression is None:
        assert "compression" not in table.components
    else:
        assert table.components["compression"] == pytest.approx(compression)


@pytest.mark.parametrize(
    "region", ["mrezhi", "bansko_razlog", "byala", "karnobat"]
)
def test_components_always_sum_to_the_total(region: str) -> None:
    """The column count varies by area, so the sum is what validates the parse."""
    table = parse_price_pdf(read(f"price_{region}_2026-09.pdf"))
    assert sum(table.components.values()) == pytest.approx(
        table.total_excl_vat, abs=0.03
    )


def test_picks_the_euro_table_not_the_legacy_lev_one() -> None:
    """Both are published during the transition; the euro row is the smaller."""
    table = parse_price_pdf(read("price_mrezhi_2026-09.pdf"))
    assert table.total_excl_vat == pytest.approx(65.89)
    assert table.total_excl_vat != pytest.approx(128.87)


def test_vat_is_applied_to_the_net_tariff() -> None:
    table = parse_price_pdf(read("price_mrezhi_2026-09.pdf"))
    assert table.total_incl_vat(20.0) == pytest.approx(79.068)


def test_row_with_components_that_do_not_add_up_is_rejected() -> None:
    with pytest.raises(ParseError, match="does not match the stated total"):
        _validate_row([18.71, 3.01, 1.74, 42.43, 99.99])


def test_unparseable_document_is_rejected() -> None:
    with pytest.raises(ParseError):
        parse_price_pdf(b"not a pdf at all")


# --- calorific value ------------------------------------------------------

@pytest.mark.parametrize(
    ("month", "expected"),
    [
        (date(2025, 10, 1), 10.79),
        (date(2026, 7, 1), 10.87),
        (date(2026, 8, 1), 10.82),
        (date(2026, 9, 1), 10.76),
    ],
)
def test_reads_the_monthly_calorific_value(
    gcv_workbook: bytes, month: date, expected: float
) -> None:
    assert parse_gcv_xlsx(gcv_workbook, month).value == pytest.approx(expected)


def test_month_from_another_gas_year_is_rejected(gcv_workbook: bytes) -> None:
    """Month names repeat, so April 2030 must not return April 2026's value."""
    with pytest.raises(ParseError, match="does not include"):
        parse_gcv_xlsx(gcv_workbook, date(2030, 4, 1))


def test_month_the_workbook_has_not_reached_yet_is_rejected(
    gcv_workbook: bytes,
) -> None:
    """The 2025-2026 workbook ends in September; October opens a new one."""
    with pytest.raises(ParseError, match="does not include"):
        parse_gcv_xlsx(gcv_workbook, date(2026, 10, 1))


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (date(2026, 9, 30), (25, 26)),
        (date(2026, 10, 1), (26, 27)),
        (date(2026, 1, 15), (25, 26)),
    ],
)
def test_gas_year_runs_october_to_september(
    moment: date, expected: tuple[int, int]
) -> None:
    assert gas_year(moment) == expected


# --- URL discovery --------------------------------------------------------

def test_finds_the_current_tariff_url() -> None:
    index = (FIXTURES / "price_index.html").read_text(errors="replace")
    period, url = find_price_url(index, "TSENA-SAJT_red")
    assert period == date(2026, 9, 1)
    assert url.endswith("/2026/09/TSENA-SAJT_red.pdf")


def test_area_slugs_do_not_collide() -> None:
    """The Sofia slug is a prefix of the others, so matching must be exact."""
    index = (FIXTURES / "price_index.html").read_text(errors="replace")
    _, sofia = find_price_url(index, "TSENA-SAJT_red")
    _, byala = find_price_url(index, "TSENA-SAJT_red-KPG-Byala")
    assert sofia != byala
    assert sofia.endswith("TSENA-SAJT_red.pdf")
    assert byala.endswith("TSENA-SAJT_red-KPG-Byala.pdf")


def test_finds_the_workbook_for_the_right_gas_year() -> None:
    index = (FIXTURES / "gcv_index.html").read_text(errors="replace")
    assert "R_GCV_25_26" in find_gcv_url(index, date(2026, 9, 1))


def test_workbook_with_a_mistyped_end_year_is_found() -> None:
    # As published for 2026-2027: the filename says 26_26.
    index = '<a href="files/useruploads/files/R_GCV_26_26October.xlsx">2026-2027</a>'
    assert find_gcv_url(index, date(2026, 10, 1)).endswith("R_GCV_26_26October.xlsx")


def test_exact_end_year_wins_over_a_mistyped_one() -> None:
    index = (
        '<a href="files/R_GCV_26_26October.xlsx">x</a>'
        '<a href="files/R_GCV_26_27October.xlsx">y</a>'
    )
    assert find_gcv_url(index, date(2026, 10, 1)).endswith("R_GCV_26_27October.xlsx")


def test_missing_gas_year_is_rejected() -> None:
    index = (FIXTURES / "gcv_index.html").read_text(errors="replace")
    with pytest.raises(ParseError, match="no calorific value workbook"):
        find_gcv_url(index, date(2031, 11, 1))


# --- end to end -----------------------------------------------------------

def test_reproduces_the_september_price_per_cubic_metre(gcv_workbook: bytes) -> None:
    """65.89 EUR/MWh net, +20% VAT, at 10.76 kWh/m3 -> 0.85077 EUR/m3."""
    table = parse_price_pdf(read("price_mrezhi_2026-09.pdf"))
    gcv = parse_gcv_xlsx(gcv_workbook, date(2026, 9, 1)).value
    assert table.total_incl_vat(20.0) / 1000 * gcv == pytest.approx(0.85077, abs=5e-5)


def test_matches_the_august_invoice(gcv_workbook: bytes) -> None:
    """A metered 21 m3 billed as 0.227220 MWh on the August 2026 invoice."""
    gcv = parse_gcv_xlsx(gcv_workbook, date(2026, 8, 1)).value
    assert 21 * gcv / 1000 == pytest.approx(0.227220, abs=1e-6)
