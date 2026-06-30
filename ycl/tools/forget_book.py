"""ycl.forget_book — remove a borrow record from the local store.

Does NOT delete corpus passages or on-disk text. The user can re-add the
book later via record_borrow / scrape_book without any cleanup.
"""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from ..borrows import BorrowStore
from ._errors import load_library


@tool(
    id="ycl.forget_book",
    description=(
        "Remove a borrow record from the local store. Does not delete corpus "
        "passages or on-disk extracted text — those are kept so the captured "
        "book remains searchable after the loan ends."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "book_id": {
                "type": "string",
                "description": "The book id whose borrow record to remove.",
            },
        },
        "required": ["book_id"],
    },
)
async def handler(
    book_id: str,
    **_clients,
) -> dict:
    info, _ = load_library()
    library_key = (info.url_name or "unknown") if info else "unknown"

    store = BorrowStore()
    removed = store.forget(library_key, book_id)
    return {
        "status": "forgotten" if removed else "not_found",
        "book_id": book_id,
        "library_id": library_key,
        "removed": removed,
    }
