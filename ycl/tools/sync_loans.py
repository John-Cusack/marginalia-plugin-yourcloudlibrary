"""ycl.sync_loans — pull the live list of active loans into BorrowStore.

Answers "what do I have out right now?" by hitting YCL's My-Books loans
endpoint and writing the *real* ``expires_at`` (the loan's ``dueDate``) into
each borrow record, clearing the ``expires_at_is_estimated`` guess. New loans
the plugin had never seen are added; loans it already knew about are updated
in place. Existing records that are no longer on loan are left untouched (use
``ycl.list_books``/``ycl.check_book`` to see their expired state).
"""

from __future__ import annotations

import structlog
from research_engine.plugins.sdk import tool

from .._time import from_iso, to_iso, utcnow
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

    synced: list[dict] = []
    for loan in loans:
        if not loan.item_id:
            log.warning("sync_loans_skipped_loan_without_item_id", loan=loan.raw)
            continue
        expires_at = _normalize_due_date(loan.due_date)
        fields: dict = {
            "title": loan.title,
            "media_type": loan.media_type,
            "loan_id": loan.loan_id,
        }
        if loan.author:
            fields["author"] = loan.author
        if expires_at is not None:
            fields["expires_at"] = expires_at
            # The whole point of the sync: this is the authoritative value.
            fields["expires_at_is_estimated"] = False
        store.upsert(library_id=library_key, book_id=loan.item_id, **fields)
        synced.append(
            {
                "book_id": loan.item_id,
                "title": loan.title,
                "author": loan.author,
                "media_type": loan.media_type,
                "expires_at": expires_at,
                "expires_at_is_estimated": expires_at is None,
                "days_remaining": store.days_remaining(
                    library_key, loan.item_id, now=now
                ),
                "can_renew": loan.can_renew,
            }
        )

    synced.sort(key=lambda b: (b.get("days_remaining") is None, b.get("days_remaining") or 0))

    return {
        "status": "synced",
        "library_id": library_key,
        "library_name": library_name,
        "active_loan_count": len(synced),
        "loans": synced,
    }
