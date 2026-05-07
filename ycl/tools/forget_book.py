"""ycl.forget_book — remove a borrow record from the local store.

Does NOT delete corpus passages or on-disk text. The user can re-add the
book later via record_borrow / scrape_book without any cleanup.
"""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from .._paths import COOKIE_PATH
from ..api.cookies import decode_config_cookie
from ..api.errors import NotAuthenticatedError
from ..borrows import BorrowStore
from ..session.cookies import CookieStore


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
    cookies = CookieStore(COOKIE_PATH).load()
    library_key = "unknown"
    if cookies:
        try:
            info = decode_config_cookie(cookies)
            library_key = info.url_name or library_key
        except NotAuthenticatedError:
            pass

    store = BorrowStore()
    removed = store.forget(library_key, book_id)
    return {
        "status": "forgotten" if removed else "not_found",
        "book_id": book_id,
        "library_id": library_key,
        "removed": removed,
    }
