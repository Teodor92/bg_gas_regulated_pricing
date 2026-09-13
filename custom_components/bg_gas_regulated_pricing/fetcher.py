"""Shared, conditional document fetching.

The cache is module level rather than per coordinator so that a user who
configures several supply areas fetches each shared document once, not once per
area. Both operators serve ETag and Last-Modified on the documents themselves,
so a poll that finds nothing new costs a 304 rather than a re-download.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from aiohttp import ClientError, ClientResponseError, ClientSession

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
        if response.status == 304:
            if cached is None:
                # Nothing was asked to be revalidated, so this is a broken
                # server. Caching the empty body it sends would make every
                # later poll revalidate into the same emptiness.
                raise ClientResponseError(
                    response.request_info,
                    response.history,
                    status=304,
                    message="304 with nothing cached to revalidate",
                )
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

    Used only as a hint: every failure degrades to None so the caller reads the
    page itself, rather than a cheap optimisation being able to fail the whole
    update.
    """
    try:
        raw = await fetch_text(session, url, timeout)
    except (ClientError, OSError) as err:
        _LOGGER.debug("Could not read change stamp from %s: %s", url, err)
        return None

    try:
        value = json.loads(raw).get("modified_gmt")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def clear_cache() -> None:
    """Drop every cached document. Used by tests."""
    _CACHE.clear()
