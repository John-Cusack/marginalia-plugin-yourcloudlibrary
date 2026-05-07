"""ycl.ingest_book — scrape (if needed) and ingest a borrowed book into the corpus."""

from __future__ import annotations

import structlog
from research_engine.plugins.sdk import tool
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker

from .._config import ConfigError
from .._config import load as load_config
from .._paths import text_path_for
from .._time import resolve_expires_at, to_iso, utcnow
from ..api import (
    AuthExpiredError,
    BookNotBorrowedError,
    NotAuthenticatedError,
    YclApiError,
    YclClient,
)
from ..api import (
    scrape_book as api_scrape_book,
)
from ..borrows import BorrowStore

log = structlog.get_logger(__name__)

READER_URL_TEMPLATE = "https://epub.yourcloudlibrary.com/read/{book_id}"


def _err(error_type: str, message: str, **extra) -> dict:
    return {"status": "error", "error_type": error_type, "message": message, **extra}


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

    try:
        client = YclClient.from_cookie_store()
    except NotAuthenticatedError as exc:
        return _err(
            "not_authenticated",
            str(exc),
            hint="Run `uv run python -m ycl.cli.login` once.",
        )

    library_key = client.library.url_name or "unknown"
    store = BorrowStore()
    now = utcnow()
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
    if rescrape or not text_path.exists():
        try:
            async with client:
                result = await api_scrape_book(
                    client, book_id, concurrency=concurrency
                )
        except AuthExpiredError as exc:
            return _err(
                "auth_expired", str(exc), hint="Re-run `python -m ycl.cli.login`."
            )
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
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
    else:
        await client.close()
        text = text_path.read_text(encoding="utf-8")
        scraped_title = next(
            (line.strip() for line in text.splitlines() if line.strip()),
            f"YCL Book {book_id}",
        )[:200]
        record = store.get(library_key, book_id) or {}
        isbn = record.get("isbn")
        chapter_count = record.get("chapter_count")

    if not text.strip():
        return _err("empty_text", "No text available.", book_id=book_id)

    resolved_expires_at, estimated = resolve_expires_at(
        explicit_expires_at=expires_at,
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
        "isbn": isbn,
        "borrowed_at": resolved_borrowed_at,
        "expires_at": resolved_expires_at,
        "expires_at_is_estimated": estimated,
        "scraped_at": to_iso(now),
        "char_count": len(text),
        "chapter_count": chapter_count,
        "source_url": READER_URL_TEMPLATE.format(book_id=book_id),
    }

    chunker = ProseWindowChunker()
    drafts = await chunker.chunk(text, metadata)

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
        isbn=isbn,
        borrowed_at=resolved_borrowed_at,
        expires_at=resolved_expires_at,
        expires_at_is_estimated=estimated,
        scraped=True,
        scraped_at=to_iso(now),
        char_count=len(text),
        chapter_count=chapter_count,
    )
    store.mark_ingested(library_key, book_id, document_id=result["document_id"])

    return {
        "status": "ingested",
        "book_id": book_id,
        "library_id": library_key,
        "title": final_title,
        "isbn": isbn,
        "document_id": result["document_id"],
        "passage_count": result["passage_count"],
        "source": source,
        "expires_at": resolved_expires_at,
        "expires_at_is_estimated": estimated,
        "days_remaining": store.days_remaining(library_key, book_id, now=now),
    }
