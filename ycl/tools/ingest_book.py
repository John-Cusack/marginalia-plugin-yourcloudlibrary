"""ycl.ingest_book — scrape (if needed) and ingest a borrowed book into the corpus."""

from __future__ import annotations

import structlog
from research_engine.plugins.sdk import tool
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker

from .._config import ConfigError
from .._config import load as load_config
from .._paths import text_path_for
from .._textcache import read_chapter_sidecar, write_text_cache
from .._time import resolve_expires_at, to_iso, utcnow
from ..api import (
    AuthExpiredError,
    BookNotBorrowedError,
    YclApiError,
)
from ..api import (
    scrape_book as api_scrape_book,
)
from ..borrows import BorrowStore
from ._common import effective_expires_at
from ._errors import RELOGIN_HINT, acquire_client
from ._errors import err as _err

log = structlog.get_logger(__name__)

READER_URL_TEMPLATE = "https://epub.yourcloudlibrary.com/read/{book_id}"


async def _chunk_with_chapters(chunker, text: str, chapters: list, metadata: dict):
    """Chunk text so each passage carries its chapter location when known.

    With chapter structure (a fresh scrape) each chapter is chunked
    independently with ``chapter_index``/``chapter_title`` merged into its
    metadata, then passage positions are renumbered into one monotonic
    document-order sequence. Without it (re-ingesting cached text, which is a
    flat blob) we fall back to chunking the whole document with base metadata.
    """
    if not chapters:
        return await chunker.chunk(text, metadata)

    drafts: list = []
    for chapter in chapters:
        chapter_meta = {
            **metadata,
            "chapter_index": chapter.index,
            "chapter_title": chapter.title,
        }
        drafts.extend(await chunker.chunk(chapter.text, chapter_meta))
    # ProseWindowChunker restarts ``position`` at 0 per call; renumber so the
    # corpus keeps a single document-order sequence across chapters.
    for position, draft in enumerate(drafts):
        draft.position = position
    return drafts


@tool(
    id="ycl.ingest_book",
    description=(
        "Scrape a YourCloudLibrary book via the YCL API (or reuse the on-disk "
        "cache) and ingest it into the corpus as document_type='ycl_book'. "
        "Idempotent: returns the existing document_id if already ingested. "
        "Requires that you've run ycl.cli.login at least once."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "book_id": {
                "type": "string",
                "description": "The book id from the reader/detail URL (e.g. 'onc5689').",
            },
            "rescrape": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, fetch fresh from the API even if a cached text file "
                    "exists on disk."
                ),
            },
            "force_reingest": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, ingest a new document even if one already exists. "
                    "Creates a duplicate; useful for re-chunking after a rescrape "
                    "or capturing a re-borrow as a separate document."
                ),
            },
            "expires_at": {"type": ["string", "null"], "default": None},
            "borrowed_at": {"type": ["string", "null"], "default": None},
            "title": {"type": ["string", "null"], "default": None},
            "concurrency": {"type": "integer", "default": 4, "minimum": 1, "maximum": 16},
        },
        "required": ["book_id"],
    },
)
async def handler(
    book_id: str,
    rescrape: bool = False,
    force_reingest: bool = False,
    expires_at: str | None = None,
    borrowed_at: str | None = None,
    title: str | None = None,
    concurrency: int = 4,
    ingestion=None,
    **_clients,
) -> dict:
    if ingestion is None:
        return _err(
            "config",
            "Ingestion client unavailable. Plugin needs permissions.ingest=true.",
        )
    try:
        cfg = load_config()
    except ConfigError as exc:
        return _err("config", str(exc))

    client, error = acquire_client()
    if error:
        return error

    library_key = client.library.url_name or "unknown"
    store = BorrowStore()
    now = utcnow()
    existing_record = store.get(library_key, book_id) or {}
    text_path = text_path_for(library_key, book_id)
    source = str(text_path.resolve())

    # Idempotency check — same convention as Kindle.
    if not force_reingest:
        try:
            existing = await ingestion.find_existing(source=source)
        except Exception as exc:
            log.warning("find_existing_failed", book_id=book_id, error=str(exc))
            existing = []
        if existing:
            doc = existing[0]
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
    scraped_chapters: list = []
    author: str | None = None
    subjects: list[str] = []
    description: str | None = None
    scrape_partial = False
    failed_chapters = 0
    if rescrape or not text_path.exists():
        try:
            async with client:
                result = await api_scrape_book(
                    client, book_id, concurrency=concurrency
                )
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
        scraped_chapters = result.chapters
        author = result.author
        subjects = result.subjects
        description = result.description
        scrape_partial = result.partial
        failed_chapters = result.failed_chapters
        write_text_cache(library_key, book_id, result)
    else:
        await client.close()
        text = text_path.read_text(encoding="utf-8")
        isbn = existing_record.get("isbn")
        # Rebuild chapter structure (and recover the title) from the cache
        # sidecar so a re-ingest off disk keeps the same per-chapter passage
        # metadata a fresh scrape would produce. Empty for pre-sidecar caches.
        cached_title, scraped_chapters = read_chapter_sidecar(
            library_key, book_id, text
        )
        chapter_count = existing_record.get("chapter_count") or (
            len(scraped_chapters) or None
        )
        # Prefer the title recorded at scrape time, then the sidecar's title,
        # then a generic placeholder. Never sniff cover/chapter junk from the
        # text body (P2.4).
        scraped_title = (
            existing_record.get("title") or cached_title or f"YCL Book {book_id}"
        )
        author = existing_record.get("author")
        subjects = existing_record.get("subjects") or []
        description = existing_record.get("description")
        # Preserve the partial flag recorded when the cache was written, so a
        # re-ingest of a truncated scrape doesn't masquerade as complete.
        scrape_partial = bool(existing_record.get("partial", False))
        failed_chapters = int(existing_record.get("failed_chapters") or 0)

    if not text.strip():
        return _err("empty_text", "No text available.", book_id=book_id)

    # Prefer an authoritative stored expiry (e.g. from ycl.sync_loans) over a
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
        "library_name": client.library.name,
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

    chunker = ProseWindowChunker()
    drafts = await _chunk_with_chapters(chunker, text, scraped_chapters, metadata)

    result = await ingestion.ingest_drafts(
        title=final_title,
        document_type="ycl_book",
        passage_drafts=drafts,
        source=source,
        metadata=metadata,
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

    return {
        # Distinct status so callers branching on it don't treat a
        # chapter-truncated book as a fully complete ingest.
        "status": "ingested_partial" if scrape_partial else "ingested",
        "book_id": book_id,
        "library_id": library_key,
        "title": final_title,
        "author": author,
        "isbn": isbn,
        "document_id": result["document_id"],
        "passage_count": result["passage_count"],
        "partial_scrape": scrape_partial,
        "failed_chapters": failed_chapters,
        "source": source,
        "expires_at": resolved_expires_at,
        "expires_at_is_estimated": estimated,
        "days_remaining": store.days_remaining(library_key, book_id, now=now),
    }
