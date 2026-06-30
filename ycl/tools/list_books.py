"""ycl.list_books — list known borrows for the current library."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from .._paths import COOKIE_PATH
from .._time import utcnow
from ..api.cookies import decode_config_cookie, session_expiry_status
from ..api.errors import NotAuthenticatedError
from ..borrows import BorrowStore
from ..session.cookies import CookieStore


@tool(
    id="ycl.list_books",
    description=(
        "List YCL books known to the plugin for the current library. Active "
        "loans by default; pass include_expired=true to see everything."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "include_expired": {
                "type": "boolean",
                "default": False,
                "description": "Include borrows whose expires_at is in the past.",
            },
        },
    },
)
async def handler(
    include_expired: bool = False,
    **_clients,
) -> dict:
    cookies = CookieStore(COOKIE_PATH).load()
    library_key = "unknown"
    library_name: str | None = None
    if cookies:
        try:
            info = decode_config_cookie(cookies)
            library_key = info.url_name or library_key
            library_name = info.name
        except NotAuthenticatedError:
            pass

    store = BorrowStore()
    now = utcnow()
    records = store.list(library_key)

    books: list[dict] = []
    active_count = 0
    expired_count = 0
    ingested_count = 0
    for record in records:
        days_remaining = store.days_remaining(
            library_key, record["book_id"], now=now
        )
        is_active = store.is_active(library_key, record["book_id"], now=now)
        if is_active:
            active_count += 1
        else:
            expired_count += 1
        if record.get("ingested"):
            ingested_count += 1

        if not include_expired and not is_active:
            continue

        books.append(
            {
                "book_id": record["book_id"],
                "title": record.get("title"),
                "isbn": record.get("isbn"),
                "borrowed_at": record.get("borrowed_at"),
                "expires_at": record.get("expires_at"),
                "expires_at_is_estimated": record.get("expires_at_is_estimated", False),
                "days_remaining": days_remaining,
                "expired": not is_active,
                "returned": bool(record.get("returned")),
                "scraped": bool(record.get("scraped")),
                "ingested": bool(record.get("ingested")),
                "document_id": record.get("document_id"),
            }
        )

    books.sort(key=lambda b: (b["expired"], b.get("days_remaining") or 0))

    out = {
        "library_id": library_key,
        "library_name": library_name,
        "count": len(books),
        "active_count": active_count,
        "expired_count": expired_count,
        "ingested_count": ingested_count,
        "books": books,
    }
    if cookies:
        out.update(session_expiry_status(cookies, now=now))
    return out
