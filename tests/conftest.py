"""Shared fixtures."""

from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

PRICE_PDF_URL = "https://www.overgas.bg/wp-content/uploads/2026/09/TSENA-SAJT_red.pdf"
GCV_XLSX_URL = (
    "https://bulgartransgaz.bg/files/useruploads/files/2026/R_GCV_25_26August.xlsx"
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the custom integration loadable in every test."""
    return


@pytest.fixture(name="published_documents")
def published_documents_fixture(aioclient_mock):
    """Serve the real September 2026 documents to the integration."""
    from custom_components.bg_gas_regulated_pricing.const import (
        GCV_INDEX_URL,
        PRICE_INDEX_URL,
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
