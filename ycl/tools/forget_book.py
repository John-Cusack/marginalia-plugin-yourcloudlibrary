"""yourcloudlibrary.forget_book — remove a borrow record from the local store.

Does NOT delete corpus passages or on-disk text. The user can re-add the
book later via record_borrow / scrape_book without any cleanup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from research_engine_sdk import tool

from .._paths import resolve_paths
from ..borrows import BorrowStore
from ._errors import load_library

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


@tool(
    id="yourcloudlibrary.forget_book",
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
    context: PluginContext | None = None,
    **_clients,
) -> dict:
    paths = resolve_paths(context)
    info, _ = load_library(paths)
    library_key = (info.url_name or "unknown") if info else "unknown"

    store = BorrowStore(paths.borrows_path)
    removed = store.forget(library_key, book_id)
    return {
        "status": "forgotten" if removed else "not_found",
        "book_id": book_id,
        "library_id": library_key,
        "removed": removed,
    }
