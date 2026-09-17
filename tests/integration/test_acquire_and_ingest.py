"""Live host-env integration tests for catalog search → acquire → ingest → return.

Three tiers, each skipped unless its prerequisites are present:

* ``test_catalog_search_live`` / ``test_provider_live`` — read-only; need only a
  YCL session. Verify the warmed-context catalog search and the
  SourceSearchProvider against the real site.
* ``test_acquire_ingest_return_live`` — exercises the FULL acquisition path
  against a real Postgres: borrow a live available book, scrape it, ingest into
  the corpus (real doc/passage/embedding rows), then return the loan. Gated on
  ``YCL_LIVE_ACQUIRE=1`` because it performs a real (momentary) borrow on the
  user's library account.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

LIVE_ACQUIRE = os.environ.get("YCL_LIVE_ACQUIRE") == "1"


async def _find_available_book(ingestion=None):
    """Find one currently-borrowable catalog book.

    When *ingestion* is given, return one NOT already in the corpus — the DB has
    a UNIQUE(content_hash, source), so a fresh borrow→ingest needs a new title.
    """
    from ycl.api.catalog import CatalogSearcher

    searcher = CatalogSearcher()
    try:
        for q in ("history", "science", "music", "cooking", "art", "garden",
                  "poetry", "travel", "biology", "finance"):
            items = await searcher.search(q, limit=25, available_only=True)
            for it in items:
                if ingestion is None:
                    return it
                existing = await ingestion.find_existing(source_pattern=it.document_id)
                if not existing:
                    return it
    finally:
        await searcher.close()
    return None


async def test_catalog_search_live(ycl_session):
    from ycl.api.catalog import CatalogSearcher

    searcher = CatalogSearcher()
    try:
        # A multi-word relevance query the suggestion endpoint cannot answer.
        items = await searcher.search("augustine confessions", limit=5)
    finally:
        await searcher.close()

    assert items, "expected catalog hits for 'augustine confessions'"
    assert all(it.document_id for it in items), "every hit must carry a documentId"
    assert any(it.matching_score > 0 for it in items)


async def test_provider_live(ycl_session):
    from research_engine.domain.source_search import SourceQuery, SourceSearchProvider

    from ycl.source_provider import YclSourceProvider

    provider = YclSourceProvider()
    try:
        assert isinstance(provider, SourceSearchProvider)  # runtime_checkable
        health = await provider.healthcheck()
        assert health["status"] == "ok"
        # Non-blocking warm: the first cold search() returns [] while warming, so
        # warm explicitly before asserting matches (the host fan-out tolerates the
        # empty first round; tests can't).
        await provider.warm()
        matches = await provider.search(SourceQuery(query="quantum mechanics"), limit=5)
    finally:
        await provider.aclose()

    assert matches, "provider should return matches"
    m = matches[0]
    assert m.plugin == "yourcloudlibrary"
    assert m.source_id  # documentId
    assert m.ingest_action and m.ingest_action.tool == "ycl.acquire_and_ingest"
    assert m.ingest_action.args["book_id"] == m.source_id
    assert m.metadata.get("corpus_source_pattern") == m.source_id


@pytest.mark.skipif(
    not LIVE_ACQUIRE,
    reason="set YCL_LIVE_ACQUIRE=1 to run the live borrow/return acquisition test",
)
async def test_acquire_borrow_and_return_safety(ycl_session, ycl_can_read, ingestion):
    """Borrow→return wiring + the no-loan-leak guarantee.

    Independent of whether the content scrape succeeds: acquire must borrow an
    available book and, whatever happens downstream, return the loan it opened
    (never leak a slot). This is the part of the path that works today.
    """
    from ycl.api.client import YclClient
    from ycl.tools.acquire_and_ingest import handler as acquire

    book = await _find_available_book(ingestion)
    assert book is not None, "no available un-ingested book found to acquire"
    book_id = book.document_id

    res = await acquire(book_id=book_id, return_after=True, ingestion=ingestion)
    assert res.get("borrowed_by_us") is True, res
    assert res.get("returned") is True, res  # returned even if ingest failed — no leak

    async with YclClient.from_cookie_store() as c:
        after = await c.get_book(book_id)
    assert after.status.upper() != "LOAN", "acquire must not leak a loan it opened"


@pytest.mark.skipif(
    not LIVE_ACQUIRE,
    reason="set YCL_LIVE_ACQUIRE=1 to run the live borrow/return acquisition test",
)
async def test_acquire_full_ingest(ycl_session, ycl_can_read, ingestion):
    """Full path incl. real corpus write: borrow → scrape → ingest → return.

    Requires a live (unexpired) reading session; skips otherwise (the fix for a
    stale session is a user re-login, not a code change).
    """
    from ycl.tools.acquire_and_ingest import handler as acquire

    book = await _find_available_book(ingestion)  # fresh, un-ingested
    assert book is not None, "no available un-ingested book found"
    book_id = book.document_id

    res = await acquire(book_id=book_id, return_after=True, ingestion=ingestion)
    assert res.get("status") == "ingested", res
    assert res.get("borrowed_by_us") is True, res
    assert res.get("returned") is True, res
    assert res.get("return_failed") is not True, res
    assert res.get("document_id"), res
    assert res.get("passage_count", 0) > 0, res

    existing = await ingestion.find_existing(source_pattern=book_id)
    assert existing, "ingested document not found via find_existing"

    # F4: after returning the loan, BorrowStore must not still report it active.
    from ycl.api.client import YclClient
    from ycl.borrows import BorrowStore

    lib = YclClient.from_cookie_store().library.url_name
    assert not BorrowStore().is_active(lib, book_id), (
        "returned loan should be reconciled to inactive in BorrowStore"
    )


@pytest.mark.skipif(
    not LIVE_ACQUIRE,
    reason="set YCL_LIVE_ACQUIRE=1 to run the live borrow/return acquisition test",
)
async def test_acquire_respects_existing_loan(ycl_session, ycl_can_read, ingestion):
    """If the user already had the book on loan, acquire must NOT auto-return it."""
    from ycl.api.client import YclClient
    from ycl.tools.acquire_and_ingest import handler as acquire

    book = await _find_available_book(ingestion)  # fresh so acquire reaches the loan check
    assert book is not None
    book_id = book.document_id

    client = YclClient.from_cookie_store()
    async with client:
        borrowed = await client.borrow(book_id)
    assert borrowed.status.upper() == "LOAN"

    try:
        res = await acquire(book_id=book_id, return_after=True, ingestion=ingestion)
        # We did not borrow it (it was already on loan) → must not return it.
        assert res.get("borrowed_by_us") is False, res
        assert res.get("returned") is False, res
        # Still on loan afterwards.
        async with YclClient.from_cookie_store() as c:
            after = await c.get_book(book_id)
        assert after.status.upper() == "LOAN", "acquire must not return a pre-existing loan"
    finally:
        # Clean up the loan we created for the test.
        async with YclClient.from_cookie_store() as c:
            await c.return_book(book_id)
