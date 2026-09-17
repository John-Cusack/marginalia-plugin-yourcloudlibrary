"""yourcloudlibrary.ingest_book — scrape (if needed) and ingest a borrowed book into the corpus."""

from __future__ import annotations

from typing import TYPE_CHECKING

from research_engine_sdk import tool

from .._paths import resolve_paths
from . import _ingest
from ._errors import err as _err

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "book_id": {
            "type": "string",
            "description": "The catalog documentId / reader book id (e.g. 'onc5689').",
        },
        "rescrape": {
            "type": "boolean",
            "default": False,
            "description": (
                "If true, fetch fresh from the API even if a cached text file exists on disk."
            ),
        },
        "force_reingest": {
            "type": "boolean",
            "default": False,
            "description": (
                "If true, ingest even if a document for this book already exists. Core still "
                "deduplicates identical content from the same source."
            ),
        },
        "expires_at": {
            "type": ["string", "null"],
            "default": None,
            "description": "ISO 8601 UTC loan expiry, if known. Overrides the stored/estimated one.",
        },
        "borrowed_at": {
            "type": ["string", "null"],
            "default": None,
            "description": "ISO 8601 UTC time the loan started, if known.",
        },
        "title": {
            "type": ["string", "null"],
            "default": None,
            "description": "Override the document title.",
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


@tool(
    id="yourcloudlibrary.ingest_book",
    description=(
        "Scrape a borrowed YourCloudLibrary book via the YCL API (or reuse the on-disk "
        "cache) and ingest it into the corpus as document_type='ycl_book'. "
        "Idempotent: returns the existing document_id if already ingested. "
        "Requires a session from research-engine-ycl-login."
    ),
    input_schema=INPUT_SCHEMA,
)
async def handler(
    book_id: str,
    rescrape: bool = False,
    force_reingest: bool = False,
    expires_at: str | None = None,
    borrowed_at: str | None = None,
    title: str | None = None,
    concurrency: int = 4,
    ingestion=None,
    context: PluginContext | None = None,
    **_clients,
) -> dict:
    if ingestion is None:
        return _err(
            "config",
            "Ingestion client unavailable. Plugin needs permissions.ingest=true.",
        )
    return await _ingest.ingest_book(
        ingestion=ingestion,
        paths=resolve_paths(context),
        book_id=book_id,
        rescrape=rescrape,
        force_reingest=force_reingest,
        expires_at=expires_at,
        borrowed_at=borrowed_at,
        title=title,
        concurrency=concurrency,
    )
