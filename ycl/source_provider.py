"""YclSourceProvider — wires YourCloudLibrary into the core cross-library
``search_sources`` fan-out as a *discovery* provider.

The core host fans one ``SourceQuery`` out to every registered provider and
merges results. YCL contributes whole-catalog matches with live borrow
availability, so the agent can find books to ingest without the user naming
each one. Read-only: each match carries an ``ingest_action`` descriptor; nothing
is borrowed here.

Self-contained auth: like the rest of the plugin, the provider reads the saved
session from disk (``~/.marginalia/plugins/yourcloudlibrary/cookies.json``) — it
needs no clients injected by the host.

Availability mapping (see docs/design/source-search-live-providers.md §10.2):
  available copy now            -> borrowable
  owned but 0 copies free       -> borrowable + metadata.hold_required
  pay-per-use only              -> purchasable
  already in corpus             -> in_corpus (filled by core via find_existing)
"""

from __future__ import annotations

import structlog

# Import from the canonical domain module (the SDK aggregate re-exports these);
# the domain module is pydantic-only and avoids pulling the full SDK surface.
from research_engine.domain.source_search import (
    Availability,
    IngestAction,
    SourceMatch,
    SourceQuery,
)

from ._paths import COOKIE_PATH
from .api.catalog import CatalogItem, CatalogSearcher
from .api.cookies import decode_config_cookie, reading_session_status
from .api.errors import NotAuthenticatedError
from .session.cookies import CookieStore

log = structlog.get_logger(__name__)

PLUGIN_NAME = "yourcloudlibrary"


def _availability(item: CatalogItem) -> tuple[Availability, dict]:
    extra: dict = {}
    # Pay-per-use is never loanable — match CatalogItem.is_available_now, which
    # excludes PPU regardless of currently_available. Checked first so an
    # available-copy count can't mislabel a PPU title as borrowable.
    if item.is_pay_per_use:
        return Availability.purchasable, extra
    if item.currently_available >= 1:
        return Availability.borrowable, extra
    if item.total_copies >= 1:
        # owned by the library but all copies out — borrowable after a hold.
        extra["hold_required"] = True
        return Availability.borrowable, extra
    return Availability.external_only, extra


def _confidence(item: CatalogItem, max_score: float) -> float:
    if max_score <= 0:
        return 0.0
    return round(min(1.0, item.matching_score / max_score), 4)


def item_to_match(item: CatalogItem, *, max_score: float) -> SourceMatch:
    availability, extra = _availability(item)
    metadata = {
        "isbn": item.isbn,
        "format": item.media_format,
        "currently_available": item.currently_available,
        "total_copies": item.total_copies,
        "is_pay_per_use": item.is_pay_per_use,
        "publisher": item.publisher,
        "language": item.language,
        "summary": item.summary[:500],
        "image": item.image,
        # Lets core mark in_corpus via IngestionOrchestrator.find_existing.
        "corpus_source_pattern": item.document_id,
        **extra,
    }
    # Discovery stays read-only; acquisition (borrow -> scrape -> ingest -> return)
    # is a separate explicit action keyed on the borrow/ingest documentId.
    ingest_action = IngestAction(
        tool="ycl.acquire_and_ingest",
        args={"book_id": item.document_id},
    )
    return SourceMatch(
        plugin=PLUGIN_NAME,
        source_id=item.document_id,
        title=item.title,
        authors=item.authors,
        year=item.year,
        availability=availability,
        confidence=_confidence(item, max_score),
        ingest_action=ingest_action,
        metadata=metadata,
    )


class YclSourceProvider:
    """SourceSearchProvider implementation for YourCloudLibrary (discovery)."""

    plugin_name = PLUGIN_NAME

    def __init__(self) -> None:
        # The provider owns its searcher for its lifetime (single owner — the
        # search_catalog tool keeps its own; no cross-ownership teardown hazard).
        self._searcher = CatalogSearcher()

    async def search(self, query: SourceQuery, *, limit: int) -> list[SourceMatch]:
        # The host runs each provider under an 8s wait_for; a cold browser warm
        # (~3–10s) would be cancelled mid-launch. So if not yet warm, kick off a
        # background warm (survives our cancellation) and contribute nothing this
        # round — the next fan-out hits a warm context.
        if not self._searcher.is_warm:
            self._searcher.ensure_warming()
            return []

        # Prefer a precise title/author phrase when the caller parsed one.
        terms = query.title or query.query
        if query.author and query.author.lower() not in (terms or "").lower():
            terms = f"{terms} {query.author}".strip()
        if not terms:
            return []
        try:
            items = await self._searcher.search(terms, limit=limit)
        except NotAuthenticatedError:
            # Surfaced as unauthenticated by the host once healthcheck lands; for
            # now, return empty rather than poisoning the fan-out.
            log.warning("ycl_source_search_unauthenticated")
            return []
        except Exception as e:  # never poison the fan-out
            log.warning("ycl_source_search_failed", error=str(e))
            return []
        if not items:
            return []
        max_score = max((it.matching_score for it in items), default=0.0)
        return [item_to_match(it, max_score=max_score) for it in items]

    async def healthcheck(self) -> dict:
        """Optional provider-owned status probe (presence-only — YCL has no cheap
        live auth check). Maps to the host's ProviderStatus once §3.3 lands."""
        cookies = CookieStore(COOKIE_PATH).load()
        if not cookies:
            return {"status": "unconfigured", "detail": "Run `python -m ycl.cli.login`."}
        try:
            info = decode_config_cookie(cookies)
        except NotAuthenticatedError as e:
            return {"status": "unauthenticated", "detail": str(e)}
        # Search/borrow can work on a stale session, but reading/ingest can't.
        # Report the reading-capability honestly so callers know to re-login.
        reading = reading_session_status(cookies)
        if not reading["ok"]:
            return {"status": "expired", "detail": reading["detail"], "library": info.name}
        return {"status": "ok", "detail": f"library={info.name}"}

    async def warm(self) -> None:
        """Block until this provider's searcher is warm. For callers/tests that
        need results on the first call (the host fan-out does not use this)."""
        await self._searcher._ensure_warm()

    async def aclose(self) -> None:
        await self._searcher.close()
