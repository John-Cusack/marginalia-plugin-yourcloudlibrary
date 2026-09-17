"""yourcloudlibrary.acquire_and_ingest — borrow (if needed) → scrape+ingest → optionally return.

This is the acquisition counterpart to the read-only ``yourcloudlibrary.search_catalog`` /
``search_sources`` discovery surface. It turns a catalog ``documentId`` into a
corpus document without the user manually borrowing first.

Recycling: by default it **returns the book after ingest** to free the library
loan slot — once the text is scraped to disk and ingested, the loan is dead
weight, so returning it lets the agent ingest far more titles than the concurrent
loan cap. Set ``return_after=false`` to keep the loan (e.g. so you can also read
it in the app).

Respects existing loans: if the book was *already* on loan before this call (you
were reading it), it is never auto-returned.

Ingestion goes through :mod:`ycl.tools._ingest`, the same boundary
``yourcloudlibrary.ingest_book`` uses. A failed return never undoes an ingest: the corpus
document stays and the result reports ``return_failed``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from research_engine_sdk import tool

from .._paths import resolve_paths
from .._time import to_iso, utcnow
from ..api import NotAuthenticatedError, YclApiError, YclClient
from ..api.client import _LOANED_STATUSES as _LOANED
from ..api.cookies import reading_session_status
from ..borrows import BorrowStore
from ..session.cookies import CookieStore
from . import _ingest
from ._errors import LOGIN_HINT
from ._errors import err as _err

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext

    from .._paths import PluginPaths

log = structlog.get_logger(__name__)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "book_id": {
            "type": "string",
            "description": (
                "The catalog documentId (e.g. 'onc5689') from yourcloudlibrary.search_catalog or "
                "search_sources. NOT the search 'id'/'bibliographicIdentifier'."
            ),
        },
        "return_after": {
            "type": "boolean",
            "default": True,
            "description": (
                "Return a loan this call opened once ingest finishes, successful or not, "
                "to free the slot. Set false to keep the book borrowed. A loan you already "
                "had is never returned."
            ),
        },
        "force_reingest": {
            "type": "boolean",
            "default": False,
            "description": "Borrow and ingest even if a document for this book already exists.",
        },
        "concurrency": {
            "type": "integer",
            "default": 4,
            "minimum": 1,
            "maximum": 16,
            "description": "Parallel chapter fetches when scraping.",
        },
    },
    "required": ["book_id"],
}


async def _return_loan(
    paths: PluginPaths, library_key: str, book_id: str
) -> tuple[bool, str | None]:
    """Best-effort return of a loan we opened. Returns (returned, error).

    Confirms success by the returned status AND, if uncertain, a follow-up
    get_book — so a return response that omits the book object doesn't produce a
    false "return failed". Retries once. On success, reconciles BorrowStore so the
    book no longer reports as an active loan.
    """
    last_error: str | None = None
    returned = False
    for _attempt in range(2):
        try:
            async with YclClient.from_cookie_store(paths.cookie_path) as c:
                rb = await c.return_book(book_id)
                if rb.status.upper() not in _LOANED:
                    returned = True
                else:
                    # Response still shows LOAN — verify directly before failing.
                    check = await c.get_book(book_id)
                    returned = check.status.upper() not in _LOANED
                    if not returned:
                        last_error = f"still on loan after return (status={rb.status!r})"
            if returned:
                break
        except YclApiError as exc:
            # A return that reports "no book object" can still have succeeded —
            # confirm via get_book before treating it as a failure.
            last_error = str(exc)
            try:
                async with YclClient.from_cookie_store(paths.cookie_path) as c:
                    check = await c.get_book(book_id)
                if check.status.upper() not in _LOANED:
                    returned = True
                    break
            except Exception as exc2:  # noqa: BLE001
                last_error = str(exc2)
        except Exception as exc:  # noqa: BLE001 — never raise from a best-effort return
            last_error = str(exc)
        if returned:
            break
    if returned:
        try:
            BorrowStore(paths.borrows_path).upsert(
                library_id=library_key,
                book_id=book_id,
                expires_at=to_iso(utcnow()),
                expires_at_is_estimated=False,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort bookkeeping
            log.warning("borrow_store_reconcile_failed", book_id=book_id, error=str(exc))
    return returned, last_error


@tool(
    id="yourcloudlibrary.acquire_and_ingest",
    description=(
        "Borrow a YourCloudLibrary book by its catalog documentId (from "
        "yourcloudlibrary.search_catalog / search_sources), scrape it, and ingest it into the "
        "corpus — then return the loan to free a slot (default). Use this to "
        "ingest a discovered book without borrowing it by hand. Idempotent: if "
        "already ingested it does nothing. Never returns a book that was already "
        "on loan before the call."
    ),
    input_schema=INPUT_SCHEMA,
)
async def handler(
    book_id: str,
    return_after: bool = True,
    force_reingest: bool = False,
    concurrency: int = 4,
    ingestion=None,
    context: PluginContext | None = None,
    **_clients,
) -> dict:
    if ingestion is None:
        return _err("config", "Ingestion client unavailable. Plugin needs permissions.ingest=true.")

    paths = resolve_paths(context)

    # Preflight: reading/scrape needs an unexpired session. The catalog is lenient
    # so we *could* borrow on a stale session — but the scrape would 401 and we'd
    # have spent a loan for nothing. Fail fast (no borrow) when reading is dead.
    cookies = CookieStore(paths.cookie_path).load()
    if cookies:
        reading = reading_session_status(cookies)
        if not reading["ok"]:
            return _err("session_expired", reading["detail"], can_read=False)

    try:
        client = YclClient.from_cookie_store(paths.cookie_path)
    except NotAuthenticatedError as exc:
        return _err("not_authenticated", str(exc), hint=LOGIN_HINT)

    library_key = client.library.url_name or "unknown"

    # Idempotency: already ingested? Skip borrow entirely.
    if not force_reingest:
        doc = await _ingest.find_ingested(ingestion, paths, library_key, book_id)
        if doc is not None:
            await client.close()
            return {
                "status": "already_ingested",
                "book_id": book_id,
                "document_id": doc["document_id"],
                "title": doc.get("title"),
                "borrowed_by_us": False,
                "returned": False,
            }

    borrowed_by_us = False
    try:
        # Inspect current loan state so we don't return a book the user is reading.
        try:
            book = await client.get_book(book_id)
        except YclApiError as exc:
            return _err("lookup_failed", f"could not read book status: {exc}", book_id=book_id)

        already_loaned = book.status.upper() in _LOANED
        if not already_loaned:
            if not (book.raw.get("canBorrow") or book.can_read):
                return _err(
                    "not_borrowable",
                    f"book is not available to borrow now (status={book.status!r}); "
                    "a hold may be required.",
                    book_id=book_id,
                    status=book.status,
                )
            try:
                book = await client.borrow(book_id)
            except YclApiError as exc:
                # Over-limit and other refusals surface here.
                return _err(
                    "borrow_failed",
                    str(exc),
                    book_id=book_id,
                    hint="The library loan limit may be reached; return a book or retry.",
                )
            # borrow() returned without raising → we may now hold a loan and MUST
            # try to return it later. Don't gate this on the reported status (a
            # non-LOAN success reply would otherwise leak the loan).
            borrowed_by_us = True
    finally:
        # The shared ingest opens its own client; close ours before delegating.
        await client.close()

    # The try/finally guarantees we return any loan WE opened even if ingest
    # raises OR the whole tool is cancelled mid-scrape (host timeout/shutdown) — a
    # borrowed-but-unusable loan must never leak. The return is shielded so it
    # survives the cancellation that triggered the finally.
    returned = False
    last_error: str | None = None
    interrupted: BaseException | None = None
    try:
        try:
            ingest_result = await _ingest.ingest_book(
                ingestion=ingestion,
                paths=paths,
                book_id=book_id,
                rescrape=False,
                force_reingest=force_reingest,
                concurrency=concurrency,
            )
        except Exception as exc:  # noqa: BLE001 — convert to an error result
            log.warning("ingest_raised", book_id=book_id, error=str(exc))
            ingest_result = _err("ingest_failed", str(exc), book_id=book_id)
    except BaseException as exc:  # CancelledError / shutdown — return loan, re-raise
        interrupted = exc
        ingest_result = _err("ingest_interrupted", type(exc).__name__, book_id=book_id)
    finally:
        if borrowed_by_us and return_after:
            try:
                returned, last_error = await asyncio.shield(
                    _return_loan(paths, library_key, book_id)
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                log.warning("auto_return_failed", book_id=book_id, error=last_error)
            if not returned:
                log.error("loan_return_failed", book_id=book_id, error=last_error)

    if interrupted is not None:
        raise interrupted

    result = {
        **ingest_result,
        "borrowed_by_us": borrowed_by_us,
        "returned": returned,
        "return_requested": return_after,
    }
    if borrowed_by_us and return_after and not returned:
        result["return_failed"] = True
        result["warning"] = (
            f"Borrowed book {book_id} could not be returned ({last_error}); "
            "the loan slot is still consumed — return it manually."
        )
    return result
