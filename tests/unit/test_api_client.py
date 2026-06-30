"""End-to-end YclClient test using httpx.MockTransport.

Exercises the 4-step manifest flow plus the auth-expired bounce-detection.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from ycl.api.client import YclClient
from ycl.api.errors import AuthExpiredError, BookNotBorrowedError, YclApiError
from ycl.api.scraper import scrape_book
from ycl.api.types import LibraryInfo

LIBRARY = LibraryInfo(
    name="Palm Beach County Library System",
    url_name="PalmBeachCountyLibrarySystem",
    library_uuid="793edfa10e6743fc8ce5cf6b1b4147bf",
    reaktor_patron_id=42,
    barcode="X0000",
    state="FL",
)
JAR = {"__session_PROD": "fake", "__config_PROD": "fake"}

ISBN = "9780310522744"
BOOK_UUID = "65706eb5-2928-4319-b2ec-77bced320b9b"
MANIFEST_URL = (
    f"https://epubservice.yourcloudlibrary.com/content/{BOOK_UUID}/manifest.json"
)
CHAPTER1_HREF = "OEBPS/chapter01.xhtml"
CHAPTER1_XHTML = (
    "<?xml version=\"1.0\"?><html><body>"
    "<h1>Chapter One</h1><p>Hello there.</p><p>Second.</p>"
    "</body></html>"
)
CHAPTER1_BODY = base64.b64encode(CHAPTER1_XHTML.encode("utf-8")).decode("ascii")


def _book_payload(*, status="LOAN", can_read=True) -> dict:
    return {
        "book": {
            "itemId": "onc5689",
            "isbn": ISBN,
            "title": "Four Views On the Church's Mission",
            "status": status,
            "canRead": can_read,
            "page": 208,
            "publisher": "Zondervan",
            "language": "en",
            "mediaType": "Epub",
        }
    }


def _make_transport(handler):
    return httpx.MockTransport(handler)


def _client_with_handler(handler):
    transport = _make_transport(handler)
    client = YclClient(cookie_jar=JAR, library_info=LIBRARY)
    # Replace the underlying transport with the mock.
    client._client = httpx.AsyncClient(
        transport=transport,
        cookies=JAR,
        follow_redirects=True,
        timeout=10.0,
        headers={"Origin": "https://epub.yourcloudlibrary.com"},
    )
    return client


async def test_get_book_parses_remix_loader():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "library/PalmBeachCountyLibrarySystem/detail/onc5689" in request.url.path
        assert request.url.params.get("_data") == "routes/library.$name.detail.$id"
        return httpx.Response(200, json=_book_payload())

    client = _client_with_handler(handler)
    async with client:
        book = await client.get_book("onc5689")
    assert book.item_id == "onc5689"
    assert book.isbn == ISBN
    assert book.status == "LOAN"
    assert book.can_read is True
    assert book.page_count == 208


async def test_get_book_raises_auth_expired_on_marketing_bounce():
    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate the marketing bounce: 200 but final URL on the home page.
        if "yourcloudlibrary.com/en/home" in str(request.url):
            return httpx.Response(200, text="<html>marketing</html>")
        return httpx.Response(
            302,
            headers={"location": "https://www.yourcloudlibrary.com/en/home.html"},
        )

    client = _client_with_handler(handler)
    with pytest.raises(AuthExpiredError):
        async with client:
            await client.get_book("onc5689")


async def test_get_manifest_two_step_resolution():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == f"/manifest/{ISBN}":
            assert request.url.params.get("catalogName") == "3m.us"
            return httpx.Response(200, text=json.dumps(MANIFEST_URL))
        if str(request.url) == MANIFEST_URL:
            return httpx.Response(
                200,
                json={
                    "@context": "https://readium.org/webpub-manifest/context.jsonld",
                    "metadata": {"title": "Four Views"},
                    "readingOrder": [
                        {"href": CHAPTER1_HREF, "type": "application/xhtml+xml"},
                        {"href": "OEBPS/ch2.xhtml", "type": "application/xhtml+xml"},
                    ],
                },
            )
        return httpx.Response(404)

    client = _client_with_handler(handler)
    async with client:
        manifest = await client.get_manifest(ISBN)
    assert manifest.book_uuid == BOOK_UUID
    assert (
        manifest.content_base_url
        == f"https://epubservice.yourcloudlibrary.com/content/{BOOK_UUID}"
    )
    assert len(manifest.reading_order) == 2
    assert manifest.reading_order[0].href == CHAPTER1_HREF


async def test_scrape_book_full_pipeline():
    """Wire all 4 steps together and verify the assembled plaintext."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "detail/onc5689" in path:
            return httpx.Response(200, json=_book_payload())
        if path == f"/manifest/{ISBN}":
            return httpx.Response(200, text=json.dumps(MANIFEST_URL))
        if str(request.url) == MANIFEST_URL:
            return httpx.Response(
                200,
                json={
                    "metadata": {"title": "Four Views"},
                    "readingOrder": [
                        {"href": CHAPTER1_HREF, "type": "application/xhtml+xml"}
                    ],
                },
            )
        if CHAPTER1_HREF in path:
            return httpx.Response(200, text=CHAPTER1_BODY)
        return httpx.Response(404, text=f"unmocked: {request.url}")

    client = _client_with_handler(handler)
    async with client:
        result = await scrape_book(client, "onc5689")
    assert result.book_id == "onc5689"
    assert result.isbn == ISBN
    assert "Chapter One" in result.text
    assert "Hello there." in result.text
    assert result.chapter_count == 1


async def test_scrape_book_raises_when_not_borrowed():
    def handler(request: httpx.Request) -> httpx.Response:
        if "detail/onc5689" in request.url.path:
            return httpx.Response(
                200, json=_book_payload(status="NONE", can_read=False)
            )
        return httpx.Response(404)

    client = _client_with_handler(handler)
    with pytest.raises(BookNotBorrowedError) as exc_info:
        async with client:
            await scrape_book(client, "onc5689")
    assert exc_info.value.status == "NONE"


async def test_get_book_raises_api_error_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = _client_with_handler(handler)
    with pytest.raises(YclApiError):
        async with client:
            await client.get_book("onc5689")
