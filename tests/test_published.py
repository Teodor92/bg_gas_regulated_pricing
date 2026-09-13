"""Tests for consuming the centrally published price data."""

from __future__ import annotations

import json

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bg_gas_regulated_pricing.const import (
    CONF_ALLOW_DIRECT,
    CONF_REGION,
    CONF_VAT_RATE,
    DOMAIN,
    PUBLISHED_URL,
    SOURCE_DIRECT,
    SOURCE_PUBLISHED,
    SOURCE_SEED,
)

from .conftest import FIXTURES, SEED


async def _setup(hass: HomeAssistant, allow_direct: bool = False) -> MockConfigEntry:
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


def _price(hass: HomeAssistant, entry: MockConfigEntry):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_price"
    )
    assert entity_id
    return hass.states.get(entity_id)


def mutate_board(**changes):
    """Return the published document with the Sofia tariff altered."""
    document = json.loads(SEED.read_text(encoding="utf-8"))
    document["months"]["2026-09"]["regions"]["mrezhi"].update(changes)
    return json.dumps(document).encode()


async def test_reads_the_published_data_by_default(
    hass: HomeAssistant, published_documents, freezer
) -> None:
    """The board is the normal source, and nothing is scraped."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    entry = await _setup(hass)

    state = _price(hass, entry)
    assert float(state.state) == pytest.approx(0.85077168, abs=5e-8)
    assert state.attributes["data_source"] == SOURCE_PUBLISHED
    assert state.attributes["stale"] is False


async def test_does_not_scrape_when_the_board_answers(
    hass: HomeAssistant, board, source_documents, freezer
) -> None:
    """Reading the board must not also hit the operators' servers."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    await _setup(hass)

    scraped = [
        str(call[1])
        for call in source_documents.mock_calls
        if "overgas.bg" in str(call[1]) or "bulgartransgaz.bg" in str(call[1])
    ]
    assert scraped == []


async def test_a_tampered_tariff_is_rejected(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A figure is not trusted merely because it is on the board.

    The value here sums correctly and sits inside the static range bounds, so
    only the month-over-month comparison can catch it.
    """
    freezer.move_to("2026-09-13T12:00:00+03:00")
    aioclient_mock.get(
        PUBLISHED_URL,
        content=mutate_board(
            total_excl_vat=22.46,
            components={"distribution": 18.71, "supply": 3.01, "extra": 0.74},
        ),
    )
    entry = await _setup(hass)
    state = _price(hass, entry)

    # The tampered figure works out at 0.29 EUR/m3 against a real 0.85. It must
    # not be served, and the shipped snapshot is used instead.
    assert float(state.state) == pytest.approx(0.85077168, abs=5e-8)
    assert state.attributes["data_source"] == SOURCE_SEED


async def test_unsupported_schema_does_not_fall_back_to_scraping(
    hass: HomeAssistant, aioclient_mock, source_documents, freezer
) -> None:
    """A newer format means update the integration, not route around it."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    document = json.loads(SEED.read_text(encoding="utf-8"))
    document["schema_version"] = 99
    aioclient_mock.get(PUBLISHED_URL, content=json.dumps(document).encode())

    await _setup(hass, allow_direct=True)

    scraped = [
        str(call[1])
        for call in source_documents.mock_calls
        if "overgas.bg" in str(call[1])
    ]
    assert scraped == [], "an unreadable version must not trigger a direct read"


async def test_falls_back_to_the_bundled_seed_on_a_fresh_install(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A new install with no network still starts with a plausible figure."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    aioclient_mock.get(PUBLISHED_URL, exc=ClientError("unreachable"))

    entry = await _setup(hass)

    state = _price(hass, entry)
    assert float(state.state) == pytest.approx(0.85077168, abs=5e-8)
    assert state.attributes["data_source"] == SOURCE_SEED


async def test_direct_read_is_used_only_when_enabled(
    hass: HomeAssistant, no_board, source_documents, freezer
) -> None:
    freezer.move_to("2026-09-13T12:00:00+03:00")
    entry = await _setup(hass, allow_direct=True)

    state = _price(hass, entry)
    assert state.attributes["data_source"] == SOURCE_DIRECT
    assert float(state.state) == pytest.approx(0.85077, abs=5e-5)


async def test_grace_window_keeps_the_first_of_the_month_quiet(
    hass: HomeAssistant, no_board, source_documents, freezer
) -> None:
    """On the 1st, a missing month is normal and must not trigger a scrape."""
    freezer.move_to("2026-10-01T02:00:00+03:00")
    await _setup(hass, allow_direct=True)

    scraped = [
        str(call[1])
        for call in source_documents.mock_calls
        if "overgas.bg" in str(call[1])
    ]
    assert scraped == []


async def test_a_stale_entry_is_checked_like_a_current_one(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """The start-of-month path must not wave a figure through unchecked.

    On the 2nd the current month is usually not published yet, so the newest
    entry is last month's. That branch runs every month, and anything it
    accepts also becomes the baseline everything after it is judged against.
    """
    freezer.move_to("2026-10-02T09:00:00+03:00")
    aioclient_mock.get(PUBLISHED_URL, content=mutate_board(
        total_excl_vat=22.46,
        components={"distribution": 18.71, "supply": 3.01, "extra": 0.74},
    ))
    entry = await _setup(hass)

    state = _price(hass, entry)
    assert float(state.state) == pytest.approx(0.85077168, abs=5e-8)
    assert state.attributes["data_source"] == SOURCE_SEED


async def test_a_consistent_but_wrong_file_is_still_caught(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """A file wrong throughout agrees with itself, so it needs an outside anchor."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    document = json.loads(SEED.read_text(encoding="utf-8"))
    bad = document["months"]["2026-09"]["regions"]["mrezhi"]
    bad["total_excl_vat"] = 22.46
    bad["components"] = {"distribution": 18.71, "supply": 3.01, "extra": 0.74}
    # A fabricated previous month that makes the tampering look like no change.
    document["months"]["2026-08"] = json.loads(
        json.dumps(document["months"]["2026-09"])
    )
    aioclient_mock.get(PUBLISHED_URL, content=json.dumps(document).encode())

    entry = await _setup(hass)
    assert _price(hass, entry).attributes["data_source"] == SOURCE_SEED


async def test_a_genuine_month_change_is_accepted(
    hass: HomeAssistant, aioclient_mock, freezer
) -> None:
    """The guard must not reject a real month-over-month move."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    board = (FIXTURES / "board_two_months.json").read_bytes()
    aioclient_mock.get(PUBLISHED_URL, content=board)

    entry = await _setup(hass)
    state = _price(hass, entry)
    assert state.attributes["data_source"] == SOURCE_PUBLISHED
    assert float(state.state) == pytest.approx(0.85077168, abs=5e-8)


async def test_does_not_walk_the_price_backwards(
    hass: HomeAssistant, aioclient_mock, freezer, hass_storage
) -> None:
    """A publisher rollback must not overwrite a newer figure we already hold."""
    freezer.move_to("2026-09-13T12:00:00+03:00")
    aioclient_mock.get(PUBLISHED_URL, content=SEED.read_bytes())
    entry = await _setup(hass)
    assert _price(hass, entry).attributes["period"] == "2026-09-01"

    # The board regresses to an older month only.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.clear_requests()
    old = json.loads(SEED.read_text(encoding="utf-8"))
    old["months"] = {"2026-07": old["months"]["2026-09"]}
    aioclient_mock.get(PUBLISHED_URL, content=json.dumps(old).encode())

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _price(hass, entry).attributes["period"] == "2026-09-01"
