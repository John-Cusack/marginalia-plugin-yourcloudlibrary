"""ycl.search_catalog — find books in the library catalog by title/author.

Solves the discoverability gap: every other tool needs the opaque ``book_id``
(e.g. ``onc5689``), which a user has no way to know up front. This tool turns a
human query into a list of ``{title, author, book_id, available}`` rows so the
returned ``book_id`` can be fed straight into ycl.scrape_book / ycl.ingest_book.
"""

from __future__ import annotations

import structlog
from research_engine.plugins.sdk import tool

from ..api import AuthExpiredError, YclApiError
from ._errors import RELOGIN_HINT, acquire_client
from ._errors import err as _err

log = structlog.get_logger(__name__)


@tool(
    id="ycl.search_catalog",
    description=(
        "Search the current library's YourCloudLibrary catalog by title, author, "
        "or keyword. Returns matching books with their book_id, which you can pass "
        "to ycl.scrape_book or ycl.ingest_book. Requires that you've run "
        "ycl.cli.login at least once."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Title, author, or keyword to search for.",
            },
            "limit": {
                "type": "integer",
                "default": 25,
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum number of results to return.",
            },
            "available_only": {
                "type": "boolean",
                "default": False,
                "description": "If true, only return titles available to borrow now.",
            },
        },
        "required": ["query"],
    },
)
async def handler(
    query: str,
    limit: int = 25,
    available_only: bool = False,
    **_clients,
) -> dict:
    if not query or not query.strip():
        return _err("invalid_input", "query is required and cannot be empty.")

    client, error = acquire_client()
    if error:
        return error

    library_key = client.library.url_name or "unknown"
    try:
        async with client:
            hits = await client.search_catalog(
                query, limit=limit, available_only=available_only
            )
    except AuthExpiredError as exc:
        return _err("auth_expired", str(exc), hint=RELOGIN_HINT)
    except YclApiError as exc:
        log.exception("search_error", query=query, error=str(exc))
        return _err("api_error", str(exc), query=query)

    return {
        "status": "success",
        "query": query,
        "library_id": library_key,
        "library_name": client.library.name,
        "count": len(hits),
        "results": [
            {
                "title": h.title,
                "author": h.author,
                "book_id": h.book_id,
                "available": h.available,
            }
            for h in hits
        ],
    }
