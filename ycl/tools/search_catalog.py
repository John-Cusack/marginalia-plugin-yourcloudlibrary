"""yourcloudlibrary.search_catalog — search the whole YourCloudLibrary catalog (not just borrows).

Read-only discovery: returns relevance-ranked catalog matches with live borrow
availability and the ``documentId`` needed to acquire each one. Pairs with
``yourcloudlibrary.acquire_and_ingest`` (or the core ``search_sources`` fan-out) to go from a
topic to ingested books without the user listing each title.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from research_engine_sdk import tool

from .._paths import resolve_paths
from ..api.catalog import CatalogSearcher
from ..api.errors import BrowserUnavailableError, NotAuthenticatedError
from ._errors import LOGIN_HINT
from ._errors import err as _err

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine_sdk import PluginContext

log = structlog.get_logger(__name__)

# This tool keeps its own long-lived warmed searcher per session file (reused
# across calls so each query doesn't relaunch Chromium). Separate from the
# provider's — single owner each.
_tool_searchers: dict[Path, CatalogSearcher] = {}


def _get_searcher(cookie_path: Path) -> CatalogSearcher:
    searcher = _tool_searchers.get(cookie_path)
    if searcher is None:
        searcher = _tool_searchers[cookie_path] = CatalogSearcher(cookie_path=cookie_path)
    return searcher


INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Title, author, or topic."},
        "limit": {
            "type": "integer",
            "default": 20,
            "minimum": 1,
            "maximum": 50,
            "description": "Maximum number of results to return.",
        },
        "available_only": {
            "type": "boolean",
            "default": False,
            "description": "Only return titles with a copy available to borrow right now.",
        },
    },
    "required": ["query"],
}


@tool(
    id="yourcloudlibrary.search_catalog",
    description=(
        "Search the entire YourCloudLibrary catalog by title, author, or topic. "
        "Returns relevance-ranked books with live availability "
        "(currently_available/total_copies, is_pay_per_use) and a documentId you "
        "can pass to yourcloudlibrary.acquire_and_ingest. Read-only — does not borrow."
    ),
    input_schema=INPUT_SCHEMA,
)
async def handler(
    query: str,
    limit: int = 20,
    available_only: bool = False,
    context: PluginContext | None = None,
    **_clients,
) -> dict:
    if not query or not query.strip():
        return _err("invalid_input", "query is required and cannot be empty.")
    # Reuse this tool's long-lived warmed searcher (it can block on a cold warm;
    # it isn't under the host's tight fan-out timeout). Do NOT close it.
    searcher = _get_searcher(resolve_paths(context).cookie_path)
    try:
        items = await searcher.search(query, limit=limit, available_only=available_only)
    except NotAuthenticatedError as exc:
        return _err("not_authenticated", str(exc), hint=LOGIN_HINT)
    except BrowserUnavailableError as exc:
        return _err("browser_unavailable", str(exc), hint=exc.hint)
    except Exception as exc:
        log.warning("search_catalog_failed", query=query, error=str(exc))
        return _err("api_error", str(exc), query=query)

    return {
        "status": "ok",
        "query": query,
        "library_id": searcher.library_slug,
        "count": len(items),
        "results": [
            {
                "book_id": it.document_id,  # documentId — for acquire_and_ingest
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
