"""ycl.record_borrow — register a borrow without scraping."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from .._config import ConfigError
from .._config import load as load_config
from .._time import resolve_expires_at, to_iso, utcnow
from ..borrows import BorrowStore
from ._errors import err as _err
from ._errors import load_library


@tool(
    id="ycl.record_borrow",
    description=(
        "Register a YourCloudLibrary borrow with its expiration, without "
        "scraping. Useful for queueing books to scrape later, or for "
        "capturing an exact expires_at read off the YCL UI."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "book_id": {
                "type": "string",
                "description": "The book id from the reader/detail URL.",
            },
            "title": {
                "type": ["string", "null"],
                "default": None,
                "description": "Optional title for the borrow record.",
            },
            "expires_at": {
                "type": ["string", "null"],
                "default": None,
                "description": "ISO 8601 UTC timestamp of loan expiration.",
            },
            "borrowed_at": {
                "type": ["string", "null"],
                "default": None,
                "description": "ISO 8601 UTC timestamp of when the loan started.",
            },
        },
        "required": ["book_id"],
    },
)
async def handler(
    book_id: str,
    title: str | None = None,
    expires_at: str | None = None,
    borrowed_at: str | None = None,
    **_clients,
) -> dict:
    try:
        cfg = load_config()
    except ConfigError as exc:
        return _err("config", str(exc))

    info, error = load_library(required=True)
    if error:
        return error

    library_key = info.url_name or "unknown"
    store = BorrowStore()
    now = utcnow()
    resolved_expires_at, estimated = resolve_expires_at(
        explicit_expires_at=expires_at,
        explicit_borrowed_at=borrowed_at,
        borrow_days=cfg.fallback_borrow_days,
        now=now,
    )
    resolved_borrowed_at = borrowed_at or to_iso(now)

    record = store.upsert(
        library_id=library_key,
        book_id=book_id,
        title=title,
        borrowed_at=resolved_borrowed_at,
        expires_at=resolved_expires_at,
        expires_at_is_estimated=estimated,
    )
    return {
        "status": "recorded",
        "book_id": book_id,
        "library_id": library_key,
        "library_name": info.name,
        "title": record.get("title"),
        "borrowed_at": resolved_borrowed_at,
        "expires_at": resolved_expires_at,
        "expires_at_is_estimated": estimated,
        "days_remaining": store.days_remaining(library_key, book_id, now=now),
    }
