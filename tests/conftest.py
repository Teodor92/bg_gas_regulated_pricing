"""Shared fixtures."""

from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

PRICE_PDF_URL = "https://www.overgas.bg/wp-content/uploads/2026/09/TSENA-SAJT_red.pdf"
GCV_XLSX_URL = (
    "https://bulgartransgaz.bg/files/useruploads/files/2026/R_GCV_25_26August.xlsx"
)
SEED = (
    pathlib.Path(__file__).parent.parent
    / "custom_components"
    / "bg_gas_regulated_pricing"
    / "seed.json"
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the custom integration loadable in every test."""
    return


@pytest.fixture(autouse=True)
def clear_document_cache():
    """Reset the module-level fetch cache so it cannot leak between tests."""
    from custom_components.bg_gas_regulated_pricing.fetcher import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture(name="board")
def board_fixture(aioclient_mock):
    """Serve the centrally published data, which is the normal source."""
    from custom_components.bg_gas_regulated_pricing.const import PUBLISHED_URL

    aioclient_mock.get(PUBLISHED_URL, content=SEED.read_bytes())
    return aioclient_mock


@pytest.fixture(name="no_board")
def no_board_fixture(aioclient_mock):
    """Make the published data unreachable, so other paths are exercised."""
    from aiohttp import ClientError

    from custom_components.bg_gas_regulated_pricing.const import PUBLISHED_URL

    aioclient_mock.get(PUBLISHED_URL, exc=ClientError("unreachable"))
    return aioclient_mock


@pytest.fixture(name="source_documents")
def source_documents_fixture(aioclient_mock):
    """Serve the real September 2026 documents the operators publish."""
    from custom_components.bg_gas_regulated_pricing.const import (
        GCV_INDEX_URL,
        PRICE_INDEX_URL,
        PRICE_MODIFIED_URL,
    )

    aioclient_mock.get(
        PRICE_MODIFIED_URL, content=b'{"modified_gmt":"2026-09-01T06:29:03"}'
    )
    aioclient_mock.get(
        PRICE_INDEX_URL, content=(FIXTURES / "price_index.html").read_bytes()
    )
    aioclient_mock.get(
        PRICE_PDF_URL, content=(FIXTURES / "price_mrezhi_2026-09.pdf").read_bytes()
    )
    aioclient_mock.get(
        GCV_INDEX_URL, content=(FIXTURES / "gcv_index.html").read_bytes()
    )
    aioclient_mock.get(
        GCV_XLSX_URL, content=(FIXTURES / "gcv_25_26.xlsx").read_bytes()
    )
    return aioclient_mock


@pytest.fixture(name="published_documents")
def published_documents_fixture(board, source_documents):
    """Everything reachable: the board and the documents behind it."""
    return source_documents
