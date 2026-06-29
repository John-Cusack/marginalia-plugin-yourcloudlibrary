"""ycl.sync_loans — pull the live list of active loans into BorrowStore.

Answers "what do I have out right now?" by hitting YCL's My-Books loans
endpoint and writing the *real* ``expires_at`` (the loan's ``dueDate``) into
each borrow record, clearing the ``expires_at_is_estimated`` guess. New loans
the plugin had never seen are added; loans it already knew about are updated
in place. Records that previously came from a sync (they carry a ``loan_id``)
but are no longer on the live list are reconciled as ``returned`` so the store
stops reporting a returned book as active. Manually-recorded books (no
``loan_id``) are left untouched.

The whole write — every active-loan upsert plus the return reconciliation —
runs inside a single ``BorrowStore.mutate()`` transaction (one locked read +
one write), rather than one rewrite per loan.
"""

from __future__ import annotations

import structlog
from research_engine.plugins.sdk import tool

from .._config import ConfigError
from .._config import load as load_config
from .._time import days_until, from_iso, resolve_expires_at, to_iso, utcnow
from ..api import (
    AuthExpiredError,
    NotAuthenticatedError,
    YclApiError,
    YclClient,
)
from ..borrows import BorrowStore

log = structlog.get_logger(__name__)


def _err(error_type: str, message: str, **extra) -> dict:
    return {"status": "error", "error_type": error_type, "message": message, **extra}


def _normalize_due_date(due_date: str) -> str | None:
    """Normalize a YCL ``dueDate`` to canonical UTC ISO 8601 (``...Z``).

    YCL returns an ISO 8601 timestamp; some deployments may instead send an
    epoch value. Accept both, and return ``None`` if the value is unusable so
    the caller can fall back to its own estimate rather than crash.
    """
    if not due_date:
        return None
    try:
        return to_iso(from_iso(due_date))
    except (ValueError, TypeError):
        pass
    # Defensive fallback: epoch seconds or milliseconds.
    try:
        from datetime import UTC, datetime

        epoch = float(due_date)
        if epoch > 1e11:  # milliseconds
            epoch /= 1000.0
        return to_iso(datetime.fromtimestamp(epoch, UTC))
    except (ValueError, TypeError, OverflowError, OSError):
        return None


@tool(
    id="ycl.sync_loans",
    description=(
        "Sync the list of currently-active YourCloudLibrary loans into the "
        "local borrow store, recording each loan's real expiration date "
        "(no estimation). Use this to answer 'what do I have checked out "
        "right now?' and to refresh expiry dates before scraping. Requires "
        "that you've run ycl.cli.login at least once."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
)
async def handler(
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
    library_name = client.library.name
    store = BorrowStore()
    now = utcnow()
    now_iso = to_iso(now)

    try:
        async with client:
            loans = await client.get_loans()
    except AuthExpiredError as exc:
        return _err(
            "auth_expired", str(exc), hint="Re-run `python -m ycl.cli.login`."
        )
    except YclApiError as exc:
        log.exception("sync_loans_api_error", error=str(exc))
        return _err("api_error", str(exc))

    live_ids = {loan.item_id for loan in loans if loan.item_id}
    synced: list[dict] = []

    # One locked transaction: upsert every active loan, then reconcile returns.
    with store.mutate() as state:
        shelf = state.setdefault(library_key, {})

        for loan in loans:
            if not loan.item_id:
                log.warning("sync_loans_skipped_loan_without_item_id", loan=loan.raw)
                continue
            # Route the dueDate through the central resolver: a parseable date
            # is authoritative (estimated=False); a missing/garbage one becomes
            # a flagged estimate so the book still reads as active, and the
            # output row below stays consistent with what we store.
            resolved_expires_at, estimated = resolve_expires_at(
                explicit_expires_at=_normalize_due_date(loan.due_date),
                explicit_borrowed_at=None,
                borrow_days=cfg.fallback_borrow_days,
                now=now,
            )
            record = shelf.get(loan.item_id, {})
            record.update(
                {
                    "library_id": library_key,
                    "book_id": loan.item_id,
                    "media_type": loan.media_type,
                    "loan_id": loan.loan_id,
                    "expires_at": resolved_expires_at,
                    "expires_at_is_estimated": estimated,
                }
            )
            if loan.author:
                record["author"] = loan.author
            # Don't overwrite a real stored title with the "Untitled" default.
            if loan.title and loan.title != "Untitled":
                record["title"] = loan.title
            # A re-borrow of a previously-returned book: it's active again.
            record.pop("returned", None)
            record.pop("returned_at", None)
            shelf[loan.item_id] = record

            synced.append(
                {
                    "book_id": loan.item_id,
                    "title": record.get("title"),
                    "author": loan.author,
                    "media_type": loan.media_type,
                    "expires_at": resolved_expires_at,
                    "expires_at_is_estimated": estimated,
                    "days_remaining": days_until(resolved_expires_at, now),
                    "can_renew": loan.can_renew,
                }
            )

        # Reconcile returns: a record that came from a prior sync (has a
        # loan_id) but is absent from the live list was returned. Leave
        # manually-recorded books (no loan_id) alone.
        returned_book_ids: list[str] = []
        for book_id, record in shelf.items():
            if (
                book_id not in live_ids
                and record.get("loan_id")
                and not record.get("returned")
            ):
                record["returned"] = True
                record["returned_at"] = now_iso
                returned_book_ids.append(book_id)

    synced.sort(key=lambda b: (b.get("days_remaining") is None, b.get("days_remaining") or 0))

    return {
        "status": "synced",
        "library_id": library_key,
        "library_name": library_name,
        "active_loan_count": len(synced),
        "returned_count": len(returned_book_ids),
        "returned_book_ids": returned_book_ids,
        "loans": synced,
    }
