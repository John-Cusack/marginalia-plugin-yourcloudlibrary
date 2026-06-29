"""ycl.check_book — report borrow + disk + corpus status for a book."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from .._paths import text_path_for
from .._time import utcnow
from ..api import (
    AuthExpiredError,
    NotAuthenticatedError,
    YclApiError,
    YclClient,
)
from ..borrows import BorrowStore


@tool(
    id="ycl.check_book",
    description=(
        "Check borrow status, expiration, scrape state, and ingestion state for "
        "a single book. Combines (a) the live YCL detail-page API to verify "
        "current loan status, (b) the local borrow store, (c) the on-disk "
        "extracted text, and (d) the corpus."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "book_id": {"type": "string", "description": "The book id to check."},
            "live": {
                "type": "boolean",
                "default": True,
                "description": (
                    "If true (default), call the YCL API for current loan "
                    "status. If false, only consult the local borrow store."
                ),
            },
        },
        "required": ["book_id"],
    },
)
async def handler(
    book_id: str,
    live: bool = True,
    ingestion=None,
    **_clients,
) -> dict:
    store = BorrowStore()
    now = utcnow()

    library_key = "unknown"
    library_name: str | None = None
    live_status: dict | None = None
    auth_warning: str | None = None

    if live:
        try:
            client = YclClient.from_cookie_store()
            library_key = client.library.url_name or library_key
            library_name = client.library.name
            try:
                async with client:
                    book = await client.get_book(book_id)
                live_status = {
                    "isbn": book.isbn,
                    "title": book.title,
                    "author": book.author,
                    "status": book.status,
                    "can_read": book.can_read,
                    "page_count": book.page_count,
                    "publisher": book.publisher,
                    "media_type": book.media_type,
                }
            except AuthExpiredError as exc:
                auth_warning = f"Auth expired: {exc}"
            except YclApiError as exc:
                auth_warning = f"API error: {exc}"
        except NotAuthenticatedError as exc:
            auth_warning = f"Not authenticated: {exc}"

    # Try to recover a library_key from BorrowStore if we couldn't go live.
    if library_key == "unknown":
        for shelf_key, shelf in (store._read_unlocked() or {}).items():
            if book_id in shelf:
                library_key = shelf_key
                break

    record = store.get(library_key, book_id)

    text_path = text_path_for(library_key, book_id)
    on_disk = text_path.exists()
    disk_info: dict | None = None
    if on_disk:
        stat = text_path.stat()
        disk_info = {
            "path": str(text_path),
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        }

    corpus_documents: list[dict] = []
    corpus_warning: str | None = None
    if ingestion is not None:
        try:
            corpus_documents = await ingestion.find_existing(source_pattern=book_id)
        except Exception as exc:
            corpus_warning = f"Corpus lookup failed: {exc}"

    borrow_block: dict | None = None
    if record is not None:
        borrow_block = {
            "title": record.get("title"),
            "isbn": record.get("isbn"),
            "borrowed_at": record.get("borrowed_at"),
            "expires_at": record.get("expires_at"),
            "expires_at_is_estimated": record.get("expires_at_is_estimated", False),
            "days_remaining": store.days_remaining(library_key, book_id, now=now),
            "expired": (
                record.get("expires_at") is not None
                and not store.is_active(library_key, book_id, now=now)
            ),
            "scraped": bool(record.get("scraped")),
            "scraped_at": record.get("scraped_at"),
            "char_count": record.get("char_count"),
            "chapter_count": record.get("chapter_count"),
            "ingested": bool(record.get("ingested")),
            "document_id": record.get("document_id"),
        }

    out: dict = {
        "book_id": book_id,
        "library_id": library_key,
        "library_name": library_name,
        "borrow": borrow_block,
        "live": live_status,
        "on_disk": on_disk,
        "disk": disk_info,
        "ingested": bool(corpus_documents)
        or (record is not None and bool(record.get("ingested"))),
        "documents": corpus_documents,
    }
    if auth_warning:
        out["auth_warning"] = auth_warning
    if corpus_warning:
        out["corpus_warning"] = corpus_warning
    return out
