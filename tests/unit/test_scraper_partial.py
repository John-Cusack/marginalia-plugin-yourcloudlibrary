"""P0.2 — partial / resilient chapter fetching in scrape_known_book.

One failed chapter used to discard the whole book (gather with no
return_exceptions). Now failures are isolated, retried, and — if only a few
remain unrecoverable (floor 5% of the book) — the book comes back with
``partial=True`` rather than hard-failing. Small books tolerate zero failures.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from ycl.api.client import YclClient
from ycl.api.errors import YclApiError
from ycl.api.scraper import scrape_book
from ycl.api.types import LibraryInfo

LIBRARY = LibraryInfo(name="L", url_name="PalmBeachCountyLibrarySystem", library_uuid="x")
JAR = {"__session_PROD": "fake", "__config_PROD": "fake"}
ISBN = "9780000000001"
BOOK_UUID = "uuid-1234"
MANIFEST_URL = f"https://epubservice.yourcloudlibrary.com/content/{BOOK_UUID}/manifest.json"


def _hrefs(n: int) -> list[str]:
    return [f"OEBPS/ch{i}.xhtml" for i in range(n)]


def _chapter_body(i: int) -> str:
    xhtml = f"<html><body><p>Chapter {i} body text.</p></body></html>"
    return base64.b64encode(xhtml.encode()).decode("ascii")


def _book_payload() -> dict:
    return {"book": {"itemId": "bk1", "isbn": ISBN, "title": "T",
                     "status": "LOAN", "canRead": True}}


def _manifest_payload(n: int) -> dict:
    return {
        "metadata": {"title": "T"},
        "readingOrder": [{"href": h, "type": "application/xhtml+xml"} for h in _hrefs(n)],
    }


def _client(handler):
    client = YclClient(cookie_jar=JAR, library_info=LIBRARY, backoff_base=0.0)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), cookies=JAR,
        follow_redirects=True, timeout=5.0,
    )
    return client


def _make_handler(n: int, fail, *, fail_once=False):
    """Build a mock handler for an ``n``-chapter book.

    ``fail`` is a set of chapter indices that return 404. If ``fail_once`` the
    failing chapters 404 only on their first hit, then succeed (transient).
    """
    hrefs = _hrefs(n)
    seen: dict[int, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "detail/bk1" in path:
            return httpx.Response(200, json=_book_payload())
        if path == f"/manifest/{ISBN}":
            return httpx.Response(200, text=json.dumps(MANIFEST_URL))
        if str(request.url) == MANIFEST_URL:
            return httpx.Response(200, json=_manifest_payload(n))
        for i, href in enumerate(hrefs):
            if href in path:
                seen[i] = seen.get(i, 0) + 1
                if i in fail and not (fail_once and seen[i] > 1):
                    return httpx.Response(404, text="gone")
                return httpx.Response(200, text=_chapter_body(i))
        return httpx.Response(404, text=f"unmocked {request.url}")

    return handler, seen


async def test_one_failed_chapter_returns_partial():
    # 40 chapters → tolerance floor(0.05*40)=2; one failure is tolerated.
    handler, _ = _make_handler(40, {2})
    client = _client(handler)
    async with client:
        result = await scrape_book(client, "bk1")
    assert result.partial is True
    assert result.failed_chapters == 1
    assert result.chapter_count == 39
    assert "Chapter 0 body text." in result.text
    assert "Chapter 2 body text." not in result.text


async def test_too_many_failures_hard_fails():
    # 3 failures > tolerance of 2 for a 40-chapter book.
    handler, _ = _make_handler(40, {1, 3, 5})
    client = _client(handler)
    with pytest.raises(YclApiError):
        async with client:
            await scrape_book(client, "bk1")


async def test_small_book_tolerates_zero_failures():
    # 5 chapters → tolerance floor(0.25)=0; a single failure hard-fails rather
    # than ingesting a book missing 20% of its content.
    handler, _ = _make_handler(5, {2})
    client = _client(handler)
    with pytest.raises(YclApiError):
        async with client:
            await scrape_book(client, "bk1")


async def test_transient_chapter_recovers_on_retry():
    # ch2 fails once (404), then succeeds — the chapter-level retry round saves it.
    handler, seen = _make_handler(40, {2}, fail_once=True)
    client = _client(handler)
    async with client:
        result = await scrape_book(client, "bk1")
    assert result.partial is False
    assert result.failed_chapters == 0
    assert result.chapter_count == 40
    assert "Chapter 2 body text." in result.text
    assert seen[2] == 2                              # failed once, retried once


async def test_all_chapters_succeed_not_partial():
    handler, _ = _make_handler(40, set())
    client = _client(handler)
    async with client:
        result = await scrape_book(client, "bk1")
    assert result.partial is False
    assert result.failed_chapters == 0
    assert result.chapter_count == 40
