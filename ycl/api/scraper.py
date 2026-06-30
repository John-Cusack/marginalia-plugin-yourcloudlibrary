"""High-level scrape orchestrator: book_id → full plain-text book."""

from __future__ import annotations

import asyncio
import math

import structlog

from .client import YclClient, assert_borrowed
from .errors import AuthExpiredError, YclApiError
from .text import xhtml_to_text
from .types import CHAPTER_SEPARATOR, Book, Chapter, Manifest, ScrapeResult

log = structlog.get_logger(__name__)


# Cap on parallel chapter fetches. The YCL CDN is fine with bursts but
# we don't want to look like a scraper. 4 in flight is brisk and polite.
_DEFAULT_CONCURRENCY = 4

# Extra whole-book rounds re-fetching only the chapters that failed. The
# client already retries individual requests (timeouts/429/5xx); this second
# layer catches failures that span a gather (e.g. a brief CDN outage).
_DEFAULT_CHAPTER_RETRY_ROUNDS = 2

# Hard ceiling on a single chapter fetch (including the client's internal
# retry/backoff). Bounds how long a chapter can occupy a concurrency slot, so a
# few pathological chapters can't stall the whole scrape into a multi-minute
# hang — they time out, get recorded as failed, and the retry round handles them.
_DEFAULT_CHAPTER_TIMEOUT = 90.0


async def scrape_book(
    client: YclClient,
    book_id: str,
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    retry_rounds: int = _DEFAULT_CHAPTER_RETRY_ROUNDS,
    chapter_timeout: float = _DEFAULT_CHAPTER_TIMEOUT,
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
    return await scrape_known_book(
        client,
        book,
        concurrency=concurrency,
        retry_rounds=retry_rounds,
        chapter_timeout=chapter_timeout,
    )


async def scrape_known_book(
    client: YclClient,
    book: Book,
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    retry_rounds: int = _DEFAULT_CHAPTER_RETRY_ROUNDS,
    chapter_timeout: float = _DEFAULT_CHAPTER_TIMEOUT,
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

    n = len(manifest.reading_order)
    sem = asyncio.Semaphore(max(1, concurrency))
    chapters: list[str | None] = [None] * n

    pending = list(range(n))
    # round 0 = initial pass, then up to retry_rounds re-tries of failures only.
    for round_no in range(max(0, retry_rounds) + 1):
        if not pending:
            break
        if round_no > 0:
            log.info("chapter_retry_round", round=round_no, retrying=len(pending))
        pending = await _fetch_chapters(
            client, manifest, sem, chapters, pending, chapter_timeout
        )

    # Whatever is still pending after the retry rounds is unrecoverable.
    # Tolerate a few (return partial text) but hard-fail if too much is missing.
    if pending:
        tolerated = _failure_tolerance(n)
        log.warning(
            "chapters_unrecoverable",
            failed=len(pending),
            total=n,
            tolerated=tolerated,
            indices=pending,
        )
        if len(pending) > tolerated:
            raise YclApiError(
                f"{len(pending)}/{n} chapters failed to fetch (tolerance {tolerated}); "
                "giving up rather than ingesting a badly truncated book."
            )

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

    # Chapters that fetched fine but parsed to nothing (cover images, nav docs)
    # are normal and not counted as failures — but a high count can mean the
    # XHTML parser missed real content, so surface it for diagnosis.
    empty_parsed = sum(
        1 for c in chapters if c is not None and not c.strip()
    )
    if empty_parsed:
        log.info("chapters_parsed_empty", count=empty_parsed, total=n)

    return ScrapeResult(
        book_id=book.item_id,
        isbn=book.isbn,
        title=book.title or manifest.title,
        chapters=structured,
        author=book.author,
        subjects=book.subjects,
        description=book.description,
        partial=bool(pending),
        failed_chapters=len(pending),
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


async def _fetch_chapters(
    client: YclClient,
    manifest: Manifest,
    sem: asyncio.Semaphore,
    chapters: list[str | None],
    indices: list[int],
    chapter_timeout: float,
) -> list[int]:
    """Fetch the given chapter ``indices`` concurrently; return the ones that failed.

    Successful chapters are written into ``chapters`` in place. An
    :class:`AuthExpiredError` is fatal for the whole book (re-login won't be
    fixed by retrying), so it propagates immediately; other errors are recorded
    per chapter so the caller can retry just those slots. ``chapter_timeout``
    caps a single fetch (and thus how long it holds a concurrency slot); a
    timeout is treated as an ordinary recoverable failure.
    """

    async def _fetch(idx: int) -> str:
        async with sem:
            coro = client.fetch_chapter_text(manifest, manifest.reading_order[idx])
            xhtml = (
                await asyncio.wait_for(coro, timeout=chapter_timeout)
                if chapter_timeout and chapter_timeout > 0
                else await coro
            )
        return xhtml_to_text(xhtml)

    results = await asyncio.gather(
        *(_fetch(i) for i in indices), return_exceptions=True
    )
    failed: list[int] = []
    for idx, result in zip(indices, results, strict=True):
        # AuthExpiredError dooms the whole book; cancellation/shutdown must
        # propagate so a cancelled tool call actually stops instead of being
        # mistaken for a recoverable per-chapter failure.
        if isinstance(result, (AuthExpiredError, asyncio.CancelledError,
                               KeyboardInterrupt, SystemExit)):
            raise result
        if isinstance(result, BaseException):
            failed.append(idx)
            log.warning("chapter_fetch_failed", idx=idx, error=str(result))
        else:
            chapters[idx] = result
    return failed


def _failure_tolerance(total: int) -> int:
    """How many unrecoverable chapters we'll drop and still return partial text.

    Up to ~5% of the book, floored (NOT rounded up to 1): on a small book a
    single missing chapter is a large fraction of the content, so books under
    ~20 chapters tolerate **zero** failures and hard-fail instead of ingesting
    a badly truncated document. A 27-chapter book tolerates one bad appendix.
    """
    return math.floor(total * 0.05)
