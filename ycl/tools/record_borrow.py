"""ycl.record_borrow — register a borrow without scraping."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from .._config import ConfigError
from .._config import load as load_config
from .._paths import COOKIE_PATH
from .._time import resolve_expires_at, to_iso, utcnow
from ..api.cookies import decode_config_cookie
from ..api.errors import NotAuthenticatedError
from ..borrows import BorrowStore
from ..session.cookies import CookieStore


def _err(error_type: str, message: str, **extra) -> dict:
    return {"status": "error", "error_type": error_type, "message": message, **extra}


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

    cookies = CookieStore(COOKIE_PATH).load()
    if not cookies:
        return _err(
            "not_authenticated",
            "No cookies on disk. Run `uv run python -m ycl.cli.login` once.",
        )
    try:
        info = decode_config_cookie(cookies)
    except NotAuthenticatedError as exc:
        return _err("not_authenticated", str(exc))

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
