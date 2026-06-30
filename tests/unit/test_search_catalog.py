"""P0.3 — catalog search.

Route confirmed live (routes/library.$name.search → 200 JSON), but the only
available session was stale so the populated item shape was never captured. These
tests pin the *parsing contract* against cloudLibrary's documented book-document
convention (itemId / title / contributors[].name / canBorrow); if the live shape
differs the fix is localized to _parse_search_results.
"""

from __future__ import annotations

import httpx
import pytest

from ycl.api.client import SEARCH_ROUTE, YclClient, _parse_search_results
from ycl.api.errors import YclApiError
from ycl.api.types import LibraryInfo

LIBRARY = LibraryInfo(name="L", url_name="PalmBeachCountyLibrarySystem", library_uuid="x")
JAR = {"__session_PROD": "fake", "__config_PROD": "fake"}


def _client(handler):
    client = YclClient(cookie_jar=JAR, library_info=LIBRARY, backoff_base=0.0)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), cookies=JAR,
        follow_redirects=True, timeout=5.0,
    )
    return client


def _doc(item_id, title, names, can_borrow):
    return {
        "itemId": item_id,
        "title": title,
        "contributors": [{"name": n} for n in names],
        "canBorrow": can_borrow,
        "isbn": "9780000000000",
        "mediaType": "Epub",
    }


def _search_payload(docs):
    # Mirrors the live loader top-level keys; hits nested under results.search.
    return {
        "results": {"search": {"query": "q", "documents": docs}},
        "categories": {},
        "segment": 1,
        "action": None,
        "advanced": False,
    }


async def test_search_catalog_parses_hits_and_sends_route():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = request.url.params.get("query")
        captured["_data"] = request.url.params.get("_data")
        docs = [
            _doc("onc5689", "Harry Potter", ["Rowling, J. K."], True),
            _doc("abc1234", "Fantastic Beasts", ["Rowling, J. K.", "Scamander, Newt"], False),
        ]
        return httpx.Response(200, json=_search_payload(docs))

    client = _client(handler)
    async with client:
        hits = await client.search_catalog("harry potter")

    assert "/library/PalmBeachCountyLibrarySystem/search" in captured["path"]
    assert captured["query"] == "harry potter"
    assert captured["_data"] == SEARCH_ROUTE
    assert len(hits) == 2
    assert hits[0].book_id == "onc5689"
    assert hits[0].title == "Harry Potter"
    assert hits[0].author == "Rowling, J. K."
    assert hits[0].available is True
    assert hits[1].author == "Rowling, J. K.; Scamander, Newt"
    assert hits[1].available is False


async def test_search_catalog_empty_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_payload([]))

    client = _client(handler)
    async with client:
        hits = await client.search_catalog("nothing matches")
    assert hits == []


async def test_search_catalog_respects_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        docs = [_doc(f"id{i}", f"Book {i}", ["Author"], True) for i in range(10)]
        return httpx.Response(200, json=_search_payload(docs))

    client = _client(handler)
    async with client:
        hits = await client.search_catalog("books", limit=3)
    assert len(hits) == 3


async def test_search_catalog_limit_zero_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        docs = [_doc(f"id{i}", f"Book {i}", ["Author"], True) for i in range(10)]
        return httpx.Response(200, json=_search_payload(docs))

    client = _client(handler)
    async with client:
        zero = await client.search_catalog("books", limit=0)
        allhits = await client.search_catalog("books", limit=None)
    assert zero == []                # limit=0 means none, not "all"
    assert len(allhits) == 10        # limit=None means unbounded


async def test_search_catalog_available_only_param_and_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("available") == "available"
        docs = [
            _doc("avail", "Available", ["A"], True),
            _doc("held", "Hold Only", ["A"], False),   # known-unavailable
        ]
        return httpx.Response(200, json=_search_payload(docs))

    client = _client(handler)
    async with client:
        hits = await client.search_catalog("q", available_only=True)
    # Server param is sent AND the known-unavailable hit is filtered out client-side.
    assert [h.book_id for h in hits] == ["avail"]


async def test_search_catalog_empty_query_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_payload([]))

    client = _client(handler)
    with pytest.raises(YclApiError):
        async with client:
            await client.search_catalog("   ")


# ----- parser-only unit tests (shape tolerance) ---------------------------


def test_parser_handles_alternate_list_key_and_author_field():
    payload = {
        "results": {
            "search": {
                # 'items' instead of 'documents', plain 'author' string.
                "items": [
                    {"itemId": "z1", "title": "Solo", "author": "Plain Author",
                     "available": True},
                ]
            }
        }
    }
    hits = _parse_search_results(payload)
    assert len(hits) == 1
    assert hits[0].author == "Plain Author"
    assert hits[0].available is True


def test_parser_falls_back_to_first_list_of_dicts():
    payload = {"results": {"search": {"weirdKey": [
        {"itemId": "q9", "title": "Edge"},
    ]}}}
    hits = _parse_search_results(payload)
    assert len(hits) == 1
    assert hits[0].book_id == "q9"
    assert hits[0].available is None        # no availability signal


def test_parser_skips_docs_without_id():
    payload = {"results": {"search": {"documents": [
        {"title": "no id here"},
        {"itemId": "ok1", "title": "Keep"},
    ]}}}
    hits = _parse_search_results(payload)
    assert [h.book_id for h in hits] == ["ok1"]


def test_parser_tolerates_garbage():
    assert _parse_search_results(None) == []
    assert _parse_search_results({}) == []
    assert _parse_search_results({"results": "nope"}) == []
    assert _parse_search_results({"results": {"search": {}}}) == []
