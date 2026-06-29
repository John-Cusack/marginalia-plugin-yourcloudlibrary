"""High-level scrape orchestrator: book_id → full plain-text book."""

from __future__ import annotations

import asyncio

import structlog

from .client import YclClient, assert_borrowed
from .text import xhtml_to_text
from .types import Book, ScrapeResult

log = structlog.get_logger(__name__)


# Cap on parallel chapter fetches. The YCL CDN is fine with bursts but
# we don't want to look like a scraper. 4 in flight is brisk and polite.
_DEFAULT_CONCURRENCY = 4


async def scrape_book(
    client: YclClient,
    book_id: str,
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ScrapeResult:
    """Fetch a borrowed book end-to-end and return its plain text.

    Order is preserved: the returned text concatenates each
    ``readingOrder`` chapter in document order, separated by blank lines.
    Chapters are fetched concurrently (bounded by ``concurrency``) but
    re-assembled deterministically.
    """
    book = await client.get_book(book_id)
    log.info(
        "book_resolved",
        book_id=book_id,
        isbn=book.isbn,
        status=book.status,
        can_read=book.can_read,
    )
    assert_borrowed(book)
    return await scrape_known_book(client, book, concurrency=concurrency)


async def scrape_known_book(
    client: YclClient,
    book: Book,
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ScrapeResult:
    """Like :func:`scrape_book` but skips the borrow-status check.

    Use when the caller has already validated the loan (e.g. for a
    forced rescrape after the loan expired but the user asked us to try
    anyway with cached cookies).
    """
    manifest = await client.get_manifest(book.isbn)
    log.info(
        "manifest_resolved",
        isbn=book.isbn,
        chapters=len(manifest.reading_order),
        book_uuid=manifest.book_uuid,
    )

    sem = asyncio.Semaphore(max(1, concurrency))
    chapters: list[str | None] = [None] * len(manifest.reading_order)

    async def _fetch(idx: int) -> None:
        async with sem:
            xhtml = await client.fetch_chapter_text(
                manifest, manifest.reading_order[idx]
            )
        chapters[idx] = xhtml_to_text(xhtml)

    await asyncio.gather(*(_fetch(i) for i in range(len(manifest.reading_order))))

    pieces = [text for text in chapters if text and text.strip()]
    full_text = "\n\n".join(pieces)

    return ScrapeResult(
        book_id=book.item_id,
        isbn=book.isbn,
        title=book.title or manifest.title,
        text=full_text,
        chapter_count=sum(1 for c in chapters if c and c.strip()),
        total_chars=len(full_text),
        author=book.author,
        subjects=book.subjects,
        description=book.description,
    )
