"""High-level scrape orchestrator: book_id → full plain-text book."""

from __future__ import annotations

import asyncio

import structlog

from .client import YclClient, assert_borrowed
from .text import xhtml_to_text
from .types import CHAPTER_SEPARATOR, Book, Chapter, Manifest, ScrapeResult

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

    # Map each reading-order href to its toc title so passages stay navigable
    # ("which chapter is this from?") instead of being a flat wall of text.
    toc_titles = _toc_titles(manifest)
    structured: list[Chapter] = []
    for idx, item in enumerate(manifest.reading_order):
        text = chapters[idx]
        if not (text and text.strip()):
            continue
        structured.append(
            Chapter(
                index=idx,
                href=item.href,
                title=toc_titles.get(_href_key(item.href)),
                text=text,
            )
        )

    return ScrapeResult(
        book_id=book.item_id,
        isbn=book.isbn,
        title=book.title or manifest.title,
        chapters=structured,
        author=book.author,
        subjects=book.subjects,
        description=book.description,
    )


def _href_key(href: str) -> str:
    """Normalize an href for matching toc entries to reading-order items.

    Drops any fragment anchor (``OEBPS/ch01.xhtml#sec2``) and a leading
    slash so a toc href and a reading-order href resolve to the same key.
    """
    return href.split("#", 1)[0].lstrip("/")


def chapter_specs(chapters: list[Chapter]) -> list[dict]:
    """Serialize chapter structure without duplicating text.

    Records each chapter's ``index``/``href``/``title`` plus its text
    ``length`` so :func:`chapters_from_specs` can slice the flat book text
    back into chapters from the on-disk cache.
    """
    return [
        {"index": c.index, "href": c.href, "title": c.title, "length": len(c.text)}
        for c in chapters
    ]


def chapters_from_specs(text: str, specs: list[dict]) -> list[Chapter]:
    """Rebuild :class:`Chapter` objects from ``text`` + :func:`chapter_specs`.

    Inverse of the :data:`CHAPTER_SEPARATOR`-join that produced ``text``:
    walk the recorded lengths, slicing each chapter and stepping past the
    separator between them.
    """
    chapters: list[Chapter] = []
    offset = 0
    for spec in specs:
        length = int(spec.get("length") or 0)
        chapters.append(
            Chapter(
                index=int(spec.get("index", len(chapters))),
                href=str(spec.get("href", "")),
                title=spec.get("title"),
                text=text[offset : offset + length],
            )
        )
        offset += length + len(CHAPTER_SEPARATOR)
    return chapters


def _toc_titles(manifest: Manifest) -> dict[str, str]:
    """Flatten the manifest ``toc`` into ``{href_without_fragment: title}``.

    Readium toc entries can nest (``children``) and carry fragment anchors
    (``OEBPS/ch01.xhtml#sec2``); we key by the bare href so they line up with
    ``readingOrder`` items. The first title wins for a given href.
    """
    out: dict[str, str] = {}

    def _walk(entries: object) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            href = entry.get("href")
            title = entry.get("title")
            if isinstance(href, str) and isinstance(title, str) and title.strip():
                out.setdefault(_href_key(href), title.strip())
            _walk(entry.get("children"))

    _walk((manifest.raw or {}).get("toc"))
    return out
