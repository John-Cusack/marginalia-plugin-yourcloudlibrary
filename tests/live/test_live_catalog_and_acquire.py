"""Live YourCloudLibrary checks: catalog search, provider, and borrow → ingest → return.

* ``test_catalog_search_live`` / ``test_provider_live`` — read-only; need only a
  saved session.
* ``test_acquire_*`` — borrow a real, currently available book on your account,
  ingest it into the disposable test database, and return it. Gated on
  ``YCL_LIVE_ACQUIRE=1`` because the borrow is real (if brief).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

needs_acquire = pytest.mark.skipif(
    os.environ.get("YCL_LIVE_ACQUIRE") != "1",
    reason="set YCL_LIVE_ACQUIRE=1 to run tests that borrow a real book",
)


async def _find_available_book(cookie_path, ingestion=None):
    """One currently borrowable book, not already in the test corpus when given."""
    from ycl.api.catalog import CatalogSearcher

    searcher = CatalogSearcher(cookie_path=cookie_path)
    try:
        for q in ("history", "science", "music", "cooking", "art", "garden",
                  "poetry", "travel", "biology", "finance"):
            for item in await searcher.search(q, limit=25, available_only=True):
                if ingestion is None or not await ingestion.find_existing(
                    source_pattern=item.document_id
                ):
                    return item
    finally:
        await searcher.close()
    return None


async def test_catalog_search_live(live_context, plugin_paths):
    from ycl.api.catalog import CatalogSearcher

    searcher = CatalogSearcher(cookie_path=plugin_paths.cookie_path)
    try:
        # A multi-word relevance query the suggestion endpoint cannot answer.
        items = await searcher.search("augustine confessions", limit=5)
    finally:
        await searcher.close()

    assert items, "expected catalog hits for 'augustine confessions'"
    assert all(it.document_id for it in items), "every hit must carry a documentId"
    assert any(it.matching_score > 0 for it in items)


async def test_provider_live(live_context):
    from research_engine_sdk import SourceMatch, SourceQuery, SourceSearchProvider

    from ycl.source_provider import YclSourceProvider

    provider = YclSourceProvider(live_context)
    try:
        assert isinstance(provider, SourceSearchProvider)
        assert (await provider.healthcheck())["status"] in {"ok", "expired"}
        # The fan-out tolerates an empty cold round; a test can't, so warm first.
        await provider.warm()
        matches = await provider.search(SourceQuery(query="quantum mechanics"), limit=5)
    finally:
        await provider.aclose()

    assert matches, "provider should return matches"
    m = matches[0]
    assert isinstance(m, SourceMatch)
    assert m.plugin == "yourcloudlibrary"
    assert m.ingest_action and m.ingest_action.tool == "yourcloudlibrary.acquire_and_ingest"
    assert m.ingest_action.args["book_id"] == m.source_id
    assert m.metadata.get("corpus_source_pattern") == m.source_id


@needs_acquire
async def test_acquire_borrows_ingests_and_returns(can_read, plugin_paths, ingestion):
    from ycl.api.client import YclClient
    from ycl.borrows import BorrowStore
    from ycl.tools.acquire_and_ingest import handler as acquire

    book = await _find_available_book(plugin_paths.cookie_path, ingestion)
    assert book is not None, "no available un-ingested book found to acquire"

    res = await acquire(book_id=book.document_id, ingestion=ingestion, context=can_read)

    assert res.get("borrowed_by_us") is True, res
    assert res.get("returned") is True, res  # no leaked loan, whatever ingest did
    assert res.get("status") == "ingested", res
    assert res.get("passage_count", 0) > 0, res
    async with YclClient.from_cookie_store(plugin_paths.cookie_path) as client:
        after = await client.get_book(book.document_id)
    assert after.status.upper() != "LOAN", "acquire must not leak a loan it opened"
    library = YclClient.from_cookie_store(plugin_paths.cookie_path).library.url_name
    assert not BorrowStore(plugin_paths.borrows_path).is_active(library, book.document_id)


@needs_acquire
async def test_acquire_respects_existing_loan(can_read, plugin_paths, ingestion):
    from ycl.api.client import YclClient
    from ycl.tools.acquire_and_ingest import handler as acquire

    book = await _find_available_book(plugin_paths.cookie_path, ingestion)
    assert book is not None
    async with YclClient.from_cookie_store(plugin_paths.cookie_path) as client:
        borrowed = await client.borrow(book.document_id)
    assert borrowed.status.upper() == "LOAN"

    try:
        res = await acquire(book_id=book.document_id, ingestion=ingestion, context=can_read)
        assert res.get("borrowed_by_us") is False, res
        assert res.get("returned") is False, res
        async with YclClient.from_cookie_store(plugin_paths.cookie_path) as client:
            after = await client.get_book(book.document_id)
        assert after.status.upper() == "LOAN", "acquire must not return a pre-existing loan"
    finally:
        async with YclClient.from_cookie_store(plugin_paths.cookie_path) as client:
            await client.return_book(book.document_id)
