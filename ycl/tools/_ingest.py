"""The plugin's single boundary into core ingestion.

``yourcloudlibrary.ingest_book`` and ``yourcloudlibrary.acquire_and_ingest`` both hand books to core
through :func:`ingest_book`, so idempotency, the on-disk cache, the borrow
registry, and the document metadata can't diverge between them.

Core chunks. The plugin sends the canonical full text plus chapter sections to
``IngestionClient.ingest_document``; core applies the ``ycl_book`` document
type's ``prose_window`` chunker from the manifest and turns the sections into
document nodes, so passages resolve to the chapter they sit in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .._config import ConfigError
from .._config import load as load_config
from .._paths import legacy_paths
from .._textcache import read_chapter_sidecar, write_text_cache
from .._time import resolve_expires_at, to_iso, utcnow
from ..api import AuthExpiredError, BookNotBorrowedError, YclApiError
from ..api import scrape_book as api_scrape_book
from ..api.types import CHAPTER_SEPARATOR
from ..borrows import BorrowStore
from ._common import effective_expires_at
from ._errors import RELOGIN_HINT, acquire_client
from ._errors import err as _err

if TYPE_CHECKING:
    from .._paths import PluginPaths
    from ..api.types import Chapter

log = structlog.get_logger(__name__)

DOCUMENT_TYPE = "ycl_book"
READER_URL_TEMPLATE = "https://epub.yourcloudlibrary.com/read/{book_id}"


def chapter_sections(text: str, chapters: list[Chapter]) -> list[dict[str, Any]]:
    """Describe each chapter as a span of ``text`` for core's node tree.

    ``text`` is the chapters joined by :data:`CHAPTER_SEPARATOR` (a fresh scrape,
    or the cache rebuilt from its sidecar). Returns ``[]`` when the chapters
    don't tile the text — a stale sidecar must degrade to a flat document, not
    to nodes pointing at the wrong prose.
    """
    sections: list[dict[str, Any]] = []
    offset = 0
    for chapter in chapters:
        start, end = offset, offset + len(chapter.text)
        if text[start:end] != chapter.text:
            log.warning("chapter_sections_misaligned", chapter_index=chapter.index)
            return []
        sections.append(
            {
                "char_start": start,
                "char_end": end,
                "level": 1,
                "heading": chapter.title,
                "node_type": "chapter",
                "chapter_index": chapter.index,
                "href": chapter.href,
            }
        )
        offset = end + len(CHAPTER_SEPARATOR)
    return sections


def candidate_sources(paths: PluginPaths, library_key: str, book_id: str) -> list[str]:
    """Source strings this book may already be ingested under, newest first.

    The source is the cache file's absolute path. Books ingested before the
    0.3.0 data-directory move recorded the legacy path, and must still count as
    ingested rather than be duplicated.
    """
    current = str(paths.text_path_for(library_key, book_id).resolve())
    legacy = str(legacy_paths().text_path_for(library_key, book_id).resolve())
    return [current] if legacy == current else [current, legacy]


async def find_ingested(
    ingestion: Any, paths: PluginPaths, library_key: str, book_id: str
) -> dict | None:
    """The existing corpus document for this book, or ``None``.

    A failed lookup is logged and treated as "not ingested": the worst case is
    a duplicate attempt, which core deduplicates by content hash and source.
    """
    for source in candidate_sources(paths, library_key, book_id):
        try:
            existing = await ingestion.find_existing(source=source)
        except Exception as exc:
            log.warning("find_existing_failed", book_id=book_id, error=str(exc))
            return None
        if existing:
            return existing[0]
    return None


async def ingest_book(
    *,
    ingestion: Any,
    paths: PluginPaths,
    book_id: str,
    rescrape: bool = False,
    force_reingest: bool = False,
    expires_at: str | None = None,
    borrowed_at: str | None = None,
    title: str | None = None,
    concurrency: int = 4,
) -> dict:
    """Scrape (or reuse the cache) and ingest one borrowed book. Idempotent."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        return _err("config", str(exc))

    client, error = acquire_client(paths)
    if error:
        return error

    library_key = client.library.url_name or "unknown"
    library_name = client.library.name
    store = BorrowStore(paths.borrows_path)
    now = utcnow()
    existing_record = store.get(library_key, book_id) or {}
    text_path = paths.text_path_for(library_key, book_id)
    source = str(text_path.resolve())

    if not force_reingest:
        doc = await find_ingested(ingestion, paths, library_key, book_id)
        if doc is not None:
            await client.close()
            return {
                "status": "already_ingested",
                "book_id": book_id,
                "library_id": library_key,
                "document_id": doc["document_id"],
                "title": doc.get("title"),
                "passage_count": doc.get("passage_count"),
                "ingested_at": doc.get("ingested_at"),
                "source": doc.get("source"),
            }

    # Acquire text — reuse cached file unless rescrape requested or no cache.
    isbn: str | None = None
    chapter_count: int | None = None
    chapters: list = []
    author: str | None = None
    subjects: list[str] = []
    description: str | None = None
    scrape_partial = False
    failed_chapters = 0
    if rescrape or not text_path.exists():
        try:
            async with client:
                result = await api_scrape_book(client, book_id, concurrency=concurrency)
        except AuthExpiredError as exc:
            return _err("auth_expired", str(exc), hint=RELOGIN_HINT)
        except BookNotBorrowedError as exc:
            store.upsert(
                library_id=library_key,
                book_id=book_id,
                expires_at=to_iso(now),
                expires_at_is_estimated=False,
            )
            return _err(
                "not_borrowed",
                f"book status={exc.status!r}; the loan may have expired.",
                book_id=book_id,
            )
        except YclApiError as exc:
            log.exception("api_error", book_id=book_id, error=str(exc))
            return _err("api_error", str(exc), book_id=book_id)

        text = result.text
        scraped_title = result.title
        isbn = result.isbn
        chapter_count = result.chapter_count
        chapters = result.chapters
        author = result.author
        subjects = result.subjects
        description = result.description
        scrape_partial = result.partial
        failed_chapters = result.failed_chapters
        write_text_cache(paths, library_key, book_id, result)
    else:
        await client.close()
        text = text_path.read_text(encoding="utf-8")
        isbn = existing_record.get("isbn")
        # Rebuild chapter structure (and recover the title) from the cache
        # sidecar so a re-ingest off disk keeps the chapter nodes a fresh scrape
        # would produce. Empty for pre-sidecar caches.
        cached_title, chapters = read_chapter_sidecar(paths, library_key, book_id, text)
        chapter_count = existing_record.get("chapter_count") or (len(chapters) or None)
        # Prefer the title recorded at scrape time, then the sidecar's title,
        # then a generic placeholder. Never sniff cover/chapter junk from the
        # text body.
        scraped_title = existing_record.get("title") or cached_title or f"YCL Book {book_id}"
        author = existing_record.get("author")
        subjects = existing_record.get("subjects") or []
        description = existing_record.get("description")
        # Preserve the partial flag recorded when the cache was written, so a
        # re-ingest of a truncated scrape doesn't masquerade as complete.
        scrape_partial = bool(existing_record.get("partial", False))
        failed_chapters = int(existing_record.get("failed_chapters") or 0)

    if not text.strip():
        return _err("empty_text", "No text available.", book_id=book_id)

    # Prefer an authoritative stored expiry (e.g. from yourcloudlibrary.sync_loans) over a
    # fresh estimate when the caller didn't pass an explicit expires_at.
    resolved_expires_at, estimated = resolve_expires_at(
        explicit_expires_at=effective_expires_at(expires_at, existing_record),
        explicit_borrowed_at=borrowed_at,
        borrow_days=cfg.fallback_borrow_days,
        now=now,
    )
    resolved_borrowed_at = borrowed_at or to_iso(now)

    final_title = title or scraped_title
    metadata = {
        "library_id": library_key,
        "library_name": library_name,
        "book_id": book_id,
        "ycl_title": final_title,
        "author": author,
        "subjects": subjects,
        "description": description,
        "isbn": isbn,
        "borrowed_at": resolved_borrowed_at,
        "expires_at": resolved_expires_at,
        "expires_at_is_estimated": estimated,
        "scraped_at": to_iso(now),
        "char_count": len(text),
        "chapter_count": chapter_count,
        "partial_scrape": scrape_partial,
        "source_url": READER_URL_TEMPLATE.format(book_id=book_id),
    }

    result = await ingestion.ingest_document(
        title=final_title,
        document_type=DOCUMENT_TYPE,
        text=text,
        source=source,
        metadata=metadata,
        sections=chapter_sections(text, chapters) or None,
    )

    store.upsert(
        library_id=library_key,
        book_id=book_id,
        title=final_title,
        author=author,
        subjects=subjects,
        description=description,
        isbn=isbn,
        borrowed_at=resolved_borrowed_at,
        expires_at=resolved_expires_at,
        expires_at_is_estimated=estimated,
        scraped=True,
        scraped_at=to_iso(now),
        char_count=len(text),
        chapter_count=chapter_count,
        partial=scrape_partial,
        failed_chapters=failed_chapters,
    )
    store.mark_ingested(library_key, book_id, document_id=result["document_id"])

    out = {
        # Distinct status so callers branching on it don't treat a
        # chapter-truncated book as a fully complete ingest.
        "status": "ingested_partial" if scrape_partial else "ingested",
        "book_id": book_id,
        "library_id": library_key,
        "title": final_title,
        "author": author,
        "isbn": isbn,
        "document_id": result["document_id"],
        "passage_count": result.get("passage_count"),
        "partial_scrape": scrape_partial,
        "failed_chapters": failed_chapters,
        "source": source,
        "expires_at": resolved_expires_at,
        "expires_at_is_estimated": estimated,
        "days_remaining": store.days_remaining(library_key, book_id, now=now),
    }
    if result.get("skipped"):
        # Core found identical content under this source and returned it.
        out["skipped"] = result["skipped"]
    return out
