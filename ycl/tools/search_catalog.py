"""ycl.search_catalog — search the whole YourCloudLibrary catalog (not just borrows).

Read-only discovery: returns relevance-ranked catalog matches with live borrow
availability and the ``documentId`` needed to acquire each one. Pairs with
``ycl.acquire_and_ingest`` (or the core ``search_sources`` fan-out) to go from a
topic to ingested books without the user listing each title.
"""

from __future__ import annotations

import structlog
from research_engine.plugins.sdk import tool

from ..api.catalog import CatalogSearcher
from ..api.errors import NotAuthenticatedError
from ._errors import LOGIN_HINT

log = structlog.get_logger(__name__)

# This tool keeps its own long-lived warmed searcher (reused across calls so each
# query doesn't relaunch Chromium). Separate from the provider's — single owner.
_tool_searcher: CatalogSearcher | None = None


def _get_searcher() -> CatalogSearcher:
    global _tool_searcher
    if _tool_searcher is None:
        _tool_searcher = CatalogSearcher()
    return _tool_searcher


@tool(
    id="ycl.search_catalog",
    description=(
        "Search the entire YourCloudLibrary catalog by title, author, or topic. "
        "Returns relevance-ranked books with live availability "
        "(currently_available/total_copies, is_pay_per_use) and a documentId you "
        "can pass to ycl.acquire_and_ingest. Read-only — does not borrow."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Title, author, or topic."},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50},
            "available_only": {
                "type": "boolean",
                "default": False,
                "description": "Only return titles with a copy available to borrow right now.",
            },
        },
        "required": ["query"],
    },
)
async def handler(
    query: str,
    limit: int = 20,
    available_only: bool = False,
    **_clients,
) -> dict:
    # Reuse this tool's long-lived warmed searcher (it can block on a cold warm;
    # it isn't under the host's tight fan-out timeout). Do NOT close it.
    searcher = _get_searcher()
    try:
        items = await searcher.search(query, limit=limit, available_only=available_only)
    except NotAuthenticatedError as exc:
        return {
            "status": "not_authenticated",
            "message": str(exc),
            "hint": LOGIN_HINT,
        }
    except Exception as exc:
        log.warning("search_catalog_failed", query=query, error=str(exc))
        return {"status": "error", "message": str(exc), "query": query}

    return {
        "status": "ok",
        "query": query,
        "library_id": searcher.library_slug,
        "count": len(items),
        "results": [
            {
                "book_id": it.document_id,            # documentId — for acquire_and_ingest
                "title": it.title,
                "subtitle": it.subtitle,
                "authors": it.authors,
                "year": it.year,
                "isbn": it.isbn,
                "format": it.media_format,
                "available_now": it.is_available_now,
                "currently_available": it.currently_available,
                "total_copies": it.total_copies,
                "is_pay_per_use": it.is_pay_per_use,
                "relevance": round(it.matching_score, 2),
                "summary": it.summary[:300],
            }
            for it in items
        ],
    }
