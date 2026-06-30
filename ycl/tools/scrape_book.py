"""ycl.scrape_book — scrape a borrowed book to disk and record the loan."""

from __future__ import annotations

import structlog
from research_engine.plugins.sdk import tool

from .._config import ConfigError
from .._config import load as load_config
from .._paths import text_path_for
from .._textcache import write_text_cache
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
from ._common import effective_expires_at

log = structlog.get_logger(__name__)


def _err(error_type: str, message: str, **extra) -> dict:
    return {"status": "error", "error_type": error_type, "message": message, **extra}


@tool(
    id="ycl.scrape_book",
    description=(
        "Scrape a borrowed YourCloudLibrary book via the YCL API and save the "
        "plain-text content to disk. Records the borrow in the local store so "
        "expiration can be tracked. Requires that you've run ycl.cli.login at "
        "least once. Does NOT ingest into the corpus — use ycl.ingest_book for "
        "that."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "book_id": {
                "type": "string",
                "description": "The book id from the reader/detail URL (e.g. 'onc5689').",
            },
            "expires_at": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "ISO 8601 UTC timestamp of loan expiration. Read off the YCL "
                    "UI when possible — preferred over borrow_days."
                ),
            },
            "borrowed_at": {
                "type": ["string", "null"],
                "default": None,
                "description": "ISO 8601 UTC timestamp of when the loan started.",
            },
            "title": {
                "type": ["string", "null"],
                "default": None,
                "description": "Override the title returned from the API.",
            },
            "concurrency": {
                "type": "integer",
                "default": 4,
                "minimum": 1,
                "maximum": 16,
                "description": "Parallel chapter fetches. 4 is brisk and polite.",
            },
        },
        "required": ["book_id"],
    },
)
async def handler(
    book_id: str,
    expires_at: str | None = None,
    borrowed_at: str | None = None,
    title: str | None = None,
    concurrency: int = 4,
    **_clients,
) -> dict:
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

    try:
        async with client:
            result = await api_scrape_book(client, book_id, concurrency=concurrency)
    except AuthExpiredError as exc:
        return _err(
            "auth_expired",
            str(exc),
            hint="Re-run `uv run python -m ycl.cli.login`.",
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
            f"book status={exc.status!r}; the loan may have expired or never existed.",
            book_id=book_id,
            library_id=library_key,
        )
    except YclApiError as exc:
        log.exception("api_error", book_id=book_id, error=str(exc))
        return _err("api_error", str(exc), book_id=book_id)

    if not result.text.strip():
        return _err(
            "empty_text",
            "API returned a manifest but every chapter parsed to empty text.",
            book_id=book_id,
            chapter_count=result.chapter_count,
        )

    write_text_cache(library_key, book_id, result)
    text_path = text_path_for(library_key, book_id)

    # Don't clobber an authoritative expiry (e.g. one ycl.sync_loans wrote)
    # with a fresh estimate when the caller didn't pass an explicit value.
    existing_record = store.get(library_key, book_id)
    resolved_expires_at, estimated = resolve_expires_at(
        explicit_expires_at=effective_expires_at(expires_at, existing_record),
        explicit_borrowed_at=borrowed_at,
        borrow_days=cfg.fallback_borrow_days,
        now=now,
    )
    resolved_borrowed_at = borrowed_at or to_iso(now)

    record = store.upsert(
        library_id=library_key,
        book_id=book_id,
        title=title or result.title,
        author=result.author,
        subjects=result.subjects,
        description=result.description,
        isbn=result.isbn,
        borrowed_at=resolved_borrowed_at,
        expires_at=resolved_expires_at,
        expires_at_is_estimated=estimated,
        scraped=True,
        scraped_at=to_iso(now),
        char_count=result.total_chars,
        chapter_count=result.chapter_count,
    )

    return {
        "status": "success",
        "book_id": book_id,
        "library_id": library_key,
        "library_name": client.library.name,
        "title": record["title"],
        "isbn": result.isbn,
        "chapter_count": result.chapter_count,
        "text_length": result.total_chars,
        "text_preview": result.text[:500],
        "output_path": str(text_path),
        "borrowed_at": resolved_borrowed_at,
        "expires_at": resolved_expires_at,
        "expires_at_is_estimated": estimated,
        "days_remaining": store.days_remaining(library_key, book_id, now=now),
    }
