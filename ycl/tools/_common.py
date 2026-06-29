"""Small helpers shared across the MCP tool handlers."""

from __future__ import annotations

from typing import Any


def effective_expires_at(
    explicit: str | None, record: dict[str, Any] | None
) -> str | None:
    """Pick the ``expires_at`` a tool should resolve from, before estimating.

    Preference order:
      1. ``explicit`` — a caller-supplied value always wins.
      2. The stored ``expires_at`` *only* when it is authoritative
         (``expires_at_is_estimated`` is falsy) — e.g. a real ``dueDate`` that
         ``ycl.sync_loans`` previously wrote. This stops scrape/ingest from
         clobbering it back to an estimate.
      3. ``None`` — let the caller fall back to its own estimate.
    """
    if explicit:
        return explicit
    record = record or {}
    stored = record.get("expires_at")
    if stored and not record.get("expires_at_is_estimated", True):
        return stored
    return None
