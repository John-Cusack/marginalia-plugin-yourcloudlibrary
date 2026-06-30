"""On-disk cache for scraped book text plus a chapter-structure sidecar.

The ``{book_id}.txt`` file stays the canonical, human-readable extract (it's
what the README documents and what ``ycl.check_book`` reports). Alongside it we
persist a compact ``{book_id}.chapters.json`` sidecar recording the book title
and per-chapter ``index``/``href``/``title``/``length``.

Why: ``ycl.ingest_book`` reuses the cached ``.txt`` instead of re-scraping, so
without the sidecar a re-ingest would chunk a flat blob and lose the
``chapter_index``/``chapter_title`` passage metadata (and the real title). The
sidecar lets the cached path rebuild the exact :class:`~ycl.api.types.Chapter`
list — and recover the title — without a network round-trip. Books scraped
before the sidecar existed simply have none; callers fall back gracefully.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from ._paths import chapters_path_for, text_path_for
from .api.scraper import chapter_specs, chapters_from_specs

if TYPE_CHECKING:
    from .api.types import Chapter, ScrapeResult

log = structlog.get_logger(__name__)


def write_text_cache(library_id: str, book_id: str, result: ScrapeResult) -> None:
    """Write the flat-text cache and its chapter-structure sidecar."""
    text_path = text_path_for(library_id, book_id)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(result.text, encoding="utf-8")
    chapters_path_for(library_id, book_id).write_text(
        json.dumps(
            {"title": result.title, "chapters": chapter_specs(result.chapters)}
        ),
        encoding="utf-8",
    )


def read_chapter_sidecar(
    library_id: str, book_id: str, text: str
) -> tuple[str | None, list[Chapter]]:
    """Reconstruct ``(title, chapters)`` from the sidecar for cached ``text``.

    Returns ``(None, [])`` when no (or an unreadable) sidecar exists, so the
    caller degrades to flat-text chunking for pre-sidecar caches.
    """
    path = chapters_path_for(library_id, book_id)
    if not path.exists():
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("chapter_sidecar_unreadable", book_id=book_id, error=str(exc))
        return None, []
    if not isinstance(data, dict):
        return None, []
    title = data.get("title") if isinstance(data.get("title"), str) else None
    specs = data.get("chapters") if isinstance(data.get("chapters"), list) else []
    return title, chapters_from_specs(text, specs)
