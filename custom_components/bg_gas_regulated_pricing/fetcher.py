"""Shared, conditional document fetching.

The cache is module level rather than per coordinator so that a user who
configures several supply areas fetches each shared document once, not once per
area. Both operators serve ETag and Last-Modified on the documents themselves,
so a poll that finds nothing new costs a 304 rather than a re-download.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiohttp import ClientResponseError, ClientSession

from .const import USER_AGENT

_LOGGER = logging.getLogger(__name__)


@dataclass
class _CachedDocument:
    body: bytes
    etag: str | None
    last_modified: str | None


_CACHE: dict[str, _CachedDocument] = {}


async def fetch(session: ClientSession, url: str, timeout: int = 60) -> bytes:
    """Return the document at url, revalidating a cached copy where possible."""
    headers = {"User-Agent": USER_AGENT}
    if (cached := _CACHE.get(url)) is not None:
        if cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified

    async with session.get(url, headers=headers, timeout=timeout) as response:
        if response.status == 304 and cached is not None:
            _LOGGER.debug("%s unchanged (304)", url)
            return cached.body
        response.raise_for_status()
        body = await response.read()

    _CACHE[url] = _CachedDocument(
        body=body,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )
    return body


async def fetch_text(session: ClientSession, url: str, timeout: int = 60) -> str:
    """Return the document at url decoded as text."""
    return (await fetch(session, url, timeout)).decode("utf-8", "replace")


async def page_modified(
    session: ClientSession, url: str, timeout: int = 30
) -> str | None:
    """Return a WordPress page's last-modified stamp, or None if unavailable.

    Used only as a hint: a caller that gets None must fall back to reading the
    page itself, and must never treat an unchanged stamp as authoritative when
    it has nothing cached to fall back on.
    """
    try:
        raw = await fetch_text(session, url, timeout)
    except (ClientResponseError, OSError) as err:
        _LOGGER.debug("Could not read change stamp from %s: %s", url, err)
        return None

    # Deliberately not json.loads: the endpoint is a hint, and a malformed or
    # unexpected response should degrade to "unknown" rather than raise.
    import json

    try:
        value = json.loads(raw).get("modified_gmt")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def clear_cache() -> None:
    """Drop every cached document. Used by tests."""
    _CACHE.clear()
