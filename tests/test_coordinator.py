"""Tests for fetching behaviour and month consistency."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bg_gas_regulated_pricing.const import (
    CONF_ALLOW_DIRECT,
    CONF_REGION,
    CONF_VAT_RATE,
    DOMAIN,
    GCV_INDEX_URL,
    PRICE_INDEX_URL,
    PRICE_MODIFIED_URL,
    PUBLISHED_URL,
)
from custom_components.bg_gas_regulated_pricing.coordinator import current_month

from .conftest import FIXTURES, GCV_XLSX_URL, PRICE_PDF_URL

NEXT_GAS_YEAR_URL = (
    "https://bulgartransgaz.bg/files/useruploads/files/2026/R_GCV_26_27September.xlsx"
)


def build_workbook(header: str, rows: list[tuple[str, float]]) -> bytes:
    """Build a minimal workbook in the shape Bulgartransgaz publish."""
    strings = [header] + [label for label, _ in rows]
    shared = "".join(f"<si><t>{s}</t></si>" for s in strings)
    shared_xml = f'<sst count="{len(strings)}">{shared}</sst>'

    body = '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
    for index, (_label, value) in enumerate(rows, start=1):
        body += (
            f'<row r="{index + 1}">'
            f'<c r="A{index + 1}" t="s"><v>{index}</v></c>'
            f'<c r="B{index + 1}"><v>{value}</v></c>'
            f"</row>"
        )
    sheet_xml = f"<worksheet><sheetData>{body}</sheetData></worksheet>"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


async def _setup(
    hass: HomeAssistant, allow_direct: bool = False
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="mrezhi",
        title="Overgas Mrezhi",
        data={
            CONF_REGION: "mrezhi",
            CONF_VAT_RATE: 20.0,
            CONF_ALLOW_DIRECT: allow_direct,
        },
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
    assert entity_id
    return hass.states.get(entity_id)


async def test_calorific_value_follows_the_tariff_month_not_today(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A late tariff must not be priced with the new month's energy content.

    On 2 October with Overgas yet to publish, the September tariff is still the
    newest one available while the 2026-2027 calorific workbook already exists.
    Pairing them would yield a price that passes the component-sum, range and
    currency checks while being wrong, and would not be flagged for two days.
    """
    freezer.move_to("2026-10-02T09:00:00+03:00")

    from aiohttp import ClientError

    # The board is unavailable, so this install reads the sources itself.
    aioclient_mock.get(PUBLISHED_URL, exc=ClientError("unreachable"))
    aioclient_mock.get(
        PRICE_MODIFIED_URL, content=b'{"modified_gmt":"2026-09-01T06:29:03"}'
    )
    # The index still advertises September: October has not been published.
    aioclient_mock.get(
        PRICE_INDEX_URL, content=(FIXTURES / "price_index.html").read_bytes()
    )
    aioclient_mock.get(
        PRICE_PDF_URL, content=(FIXTURES / "price_mrezhi_2026-09.pdf").read_bytes()
    )
    # Both gas years are available, as they are in mid-September onward.
    aioclient_mock.get(
        GCV_INDEX_URL,
        content=(
            b'<a href="files/useruploads/files/2026/R_GCV_25_26August.xlsx">25-26</a>'
            b'<a href="files/useruploads/files/2026/'
            b'R_GCV_26_27September.xlsx">26-27</a>'
        ),
    )
    aioclient_mock.get(
        GCV_XLSX_URL, content=(FIXTURES / "gcv_25_26.xlsx").read_bytes()
    )
    aioclient_mock.get(
        NEXT_GAS_YEAR_URL,
        content=build_workbook("месец/month 2026-2027", [("October/Октомври", 11.02)]),
    )

    entry = await _setup(hass, allow_direct=True)

    # September's tariff paired with September's 10.76, never October's 11.02.
    assert float(_state(hass, entry, "calorific_value").state) == pytest.approx(10.76)
    assert float(_state(hass, entry, "price").state) == pytest.approx(0.85077, abs=5e-5)

    attributes = _state(hass, entry, "price").attributes
    assert attributes["period"] == "2026-09-01"
    assert attributes["stale"] is True


async def test_fresh_month_is_not_marked_stale(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    freezer.move_to("2026-09-13T12:00:00+03:00")
    entry = await _setup(hass)
    assert _state(hass, entry, "price").attributes["stale"] is False


async def test_unchanged_page_stamp_avoids_refetching_the_index(
    hass: HomeAssistant, no_board, source_documents, freezer
) -> None:
    """The 71-byte change stamp should short-circuit the 19 KB index fetch."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    entry = await _setup(hass, allow_direct=True)
    coordinator = entry.runtime_data

    def index_fetches() -> int:
        return sum(
            1
            for call in source_documents.mock_calls
            if str(call[1]) == PRICE_INDEX_URL
        )

    before = index_fetches()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert index_fetches() == before


async def test_month_boundary_uses_bulgarian_time(
    hass: HomeAssistant, freezer
) -> None:
    """23:30 UTC on 30 September is already October in Sofia."""
    freezer.move_to("2026-09-30T23:30:00+00:00")
    assert current_month() == date(2026, 10, 1)


async def test_last_known_good_survives_a_restart_during_an_outage(
    hass: HomeAssistant, aioclient_mock, published_documents, freezer, hass_storage
) -> None:
    """A restart while the sources are down must not leave energy uncosted.

    Home Assistant cannot cost consumption retrospectively, so a coordinator
    that comes back with nothing and raises ConfigEntryNotReady loses the cost
    of everything burned until the sources recover.
    """
    from aiohttp import ClientError
    from homeassistant.config_entries import ConfigEntryState

    freezer.move_to("2026-09-13T12:00:00+03:00")
    entry = await _setup(hass)
    assert float(_state(hass, entry, "price").state) == pytest.approx(0.85077, abs=5e-5)

    # The figures were persisted, not just held in memory.
    stored = [v for k, v in hass_storage.items() if DOMAIN in k]
    assert stored, "nothing was written to storage"
    assert stored[0]["data"]["period"] == "2026-09-01"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Both operators are now unreachable.
    aioclient_mock.clear_requests()
    for url in (PUBLISHED_URL, PRICE_MODIFIED_URL, PRICE_INDEX_URL, GCV_INDEX_URL):
        aioclient_mock.get(url, exc=ClientError("unreachable"))

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert float(_state(hass, entry, "price").state) == pytest.approx(0.85077, abs=5e-5)
    assert _state(hass, entry, "price").attributes["stale"] is True
