"""Catalog (whole-library) search for YourCloudLibrary.

Unlike the borrow-centric rest of the plugin, this searches the *entire* library
catalog — the discovery surface the agent uses to find books to ingest without
the user naming each one.

Why a browser, not httpx: the relevance search loader
(``routes/library.$name.search``) only returns populated results to a *warmed*
browser context. A cold ``httpx``/``ctx.request`` call — even with byte-identical
URL, params, cookies, and browser headers — comes back empty. Loading any catalog
page first establishes the session state the loader requires. So ``CatalogSearcher``
holds one persistent Playwright context, loads ``/featured`` once, and then issues
cheap ``ctx.request`` GETs per query (no per-search page render). See
``IMPL_NOTES.md`` ("Catalog search") for the measurements.

The id that matters: each result's ``documentId`` is the detail/borrow/ingest
``book_id``. The other ids (``id``/``bibliographicIdentifier``/``catalogItemId``)
do NOT work against the detail route — they 500.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import structlog

from ..session.cookies import CookieStore
from .cookies import decode_config_cookie
from .errors import (
    LOGIN_COMMAND,
    PLAYWRIGHT_INSTALL_COMMAND,
    BrowserUnavailableError,
    NotAuthenticatedError,
    browser_launch_error,
)

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

EBOOK_HOST = "https://ebook.yourcloudlibrary.com"
_SEARCH_DATA = "routes/library.$name.search"

# Playwright cookie sanitization: the stored jar may carry extra keys / a
# non-canonical sameSite that ``add_cookies`` rejects.
_ALLOWED_COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
_SAMESITE = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None", "unspecified": "Lax"}


def _build_search_url(slug: str, query: str, *, available_only: bool = False) -> str:
    """Build the catalog search loader URL, percent-encoding the querystring so
    reserved chars in *query* (``&``, ``#``, ``=``, spaces) don't corrupt it."""
    params = urlencode(
        {
            "query": query,
            "format": "",
            "available": "available" if available_only else "any",
            "language": "",
            "sort": "",
            "orderBy": "relevence",
            "owned": "yes",
            "_data": _SEARCH_DATA,
        }
    )
    return f"{EBOOK_HOST}/library/{slug}/search?{params}"


def _sanitize_cookies(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in raw:
        d = {k: v for k, v in c.items() if k in _ALLOWED_COOKIE_KEYS}
        d["sameSite"] = _SAMESITE.get(str(d.get("sameSite", "Lax")).lower(), "Lax")
        if isinstance(d.get("expires"), float):
            d["expires"] = int(d["expires"])
        out.append(d)
    return out


@dataclass
class CatalogItem:
    """One catalog search hit. ``document_id`` is the borrow/ingest book_id."""

    document_id: str
    isbn: str
    title: str
    authors: list[str] = field(default_factory=list)
    subtitle: str = ""
    year: int | None = None
    media_format: str = ""          # "digital" (ebook) / "audio"
    summary: str = ""
    language: str = ""
    publisher: str = ""
    total_copies: int = 0
    currently_available: int = 0
    currently_loaned: int = 0
    is_pay_per_use: bool = False
    matching_score: float = 0.0
    image: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_available_now(self) -> bool:
        return self.currently_available >= 1 and not self.is_pay_per_use

    @classmethod
    def from_search_item(cls, it: dict) -> CatalogItem:
        contribs = it.get("authors") or [
            c.get("name") for c in (it.get("contributors") or []) if isinstance(c, dict)
        ]
        return cls(
            document_id=str(it.get("documentId") or ""),
            isbn=str(it.get("isbn") or ""),
            title=it.get("title") or "Untitled",
            subtitle=it.get("subtitle") or "",
            authors=[a for a in contribs if a],
            year=it.get("yearPublished"),
            media_format=it.get("format") or "",
            summary=it.get("summary") or "",
            language=it.get("language") or "",
            publisher=it.get("publisherName") or "",
            total_copies=int(it.get("totalCopies") or 0),
            currently_available=int(it.get("currentlyAvailable") or 0),
            currently_loaned=int(it.get("currentlyLoaned") or 0),
            is_pay_per_use=bool(it.get("isPayPerUse")),
            matching_score=float(it.get("matchingScore") or 0.0),
            image=it.get("imageLinkThumbnail"),
            raw=it,
        )


class CatalogSearcher:
    """Persistent warmed Playwright context for catalog search.

    Lazily launches a headless Chromium, injects the saved session cookies, and
    loads ``/featured`` once to warm the loader state. Reuse one instance across
    searches; call :meth:`close` when done. Not safe for concurrent ``search``
    calls on the same instance (one shared page/context) — callers should
    serialize or pool.
    """

    def __init__(self, *, cookie_path: Path, headless: bool = True) -> None:
        self._cookie_path = cookie_path
        self._headless = headless
        self._library_slug: str | None = None
        self._pw = None
        self._browser = None
        self._ctx = None
        self._warm = False
        self._lock = asyncio.Lock()          # guards the warm sequence
        self._search_lock = asyncio.Lock()   # serializes ctx.request searches
        self._warm_task: asyncio.Task | None = None

    @property
    def is_warm(self) -> bool:
        return self._warm

    @property
    def library_slug(self) -> str:
        if not self._library_slug:
            raise RuntimeError("CatalogSearcher not started; call search() first")
        return self._library_slug

    def ensure_warming(self) -> None:
        """Start a background warm if not already warm/warming. Non-blocking.

        Lets a caller under a tight timeout (the provider, run inside the host's
        8s ``search_sources`` wait_for) trigger the multi-second warm without
        being cancelled mid-launch — the task runs independently and survives the
        caller's cancellation, so the *next* call finds a warm context.
        """
        if self._warm or (self._warm_task is not None and not self._warm_task.done()):
            return

        async def _warm() -> None:
            try:
                await self._ensure_warm()
            except Exception as exc:  # noqa: BLE001 — background task; surface via logs
                log.warning("catalog_background_warm_failed", error=str(exc))

        self._warm_task = asyncio.ensure_future(_warm())

    async def _ensure_warm(self) -> None:
        if self._warm:
            return
        async with self._lock:
            if self._warm:
                return
            cookies = CookieStore(self._cookie_path).load()
            if not cookies:
                raise NotAuthenticatedError(
                    f"No cookie file at {self._cookie_path}. Run `{LOGIN_COMMAND}` once."
                )
            self._library_slug = decode_config_cookie(cookies).url_name
            if not self._library_slug:
                raise NotAuthenticatedError("library url_name missing — cookie malformed")

            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise BrowserUnavailableError(
                    "Playwright is not installed.", hint=PLAYWRIGHT_INSTALL_COMMAND
                ) from exc

            # BaseException (incl. asyncio.CancelledError, if a caller ever awaits
            # this under a timeout) must not leave a half-built browser orphaned.
            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=self._headless)
                self._ctx = await self._browser.new_context(
                    viewport={"width": 1280, "height": 900}
                )
                await self._ctx.add_cookies(_sanitize_cookies(cookies))
                page = await self._ctx.new_page()
                # Warm the loader state — required, see module docstring.
                await page.goto(
                    f"{EBOOK_HOST}/library/{self._library_slug}/featured",
                    wait_until="networkidle",
                    timeout=45000,
                )
                await page.close()
            except BaseException as exc:
                await self.close()  # tear down partials, reset _warm
                missing = browser_launch_error(exc)
                if missing is not None:
                    raise missing from exc
                raise
            self._warm = True
            log.debug("catalog_searcher_warm", library=self._library_slug)

    async def search(
        self, query: str, *, limit: int = 20, available_only: bool = False
    ) -> list[CatalogItem]:
        """Relevance-ranked catalog search. Returns up to ``limit`` items."""
        await self._ensure_warm()
        url = _build_search_url(self.library_slug, query, available_only=available_only)
        # Serialize concurrent searches: one shared context/state per instance.
        async with self._search_lock:
            resp = await self._ctx.request.get(url)
        if resp.status in (401, 403):
            # A lost/rotated session — surface as auth (callers branch on this to
            # tell the user to re-login) rather than masking it as "no results".
            raise NotAuthenticatedError(
                f"catalog search returned {resp.status} — session may be invalid; "
                f"re-run `{LOGIN_COMMAND}`."
            )
        if not resp.ok:
            log.warning("catalog_search_http", status=resp.status, query=query)
            return []
        try:
            body = await resp.json()
        except Exception:
            return []
        items = (((body or {}).get("results") or {}).get("search") or {}).get("items") or []
        out = [CatalogItem.from_search_item(it) for it in items if it.get("documentId")]
        if available_only:
            out = [c for c in out if c.is_available_now]
        return out[:limit]

    async def close(self) -> None:
        # Cancel an in-flight background warm first, so it can't relaunch a
        # browser after we've torn down (orphaned Chromium).
        task = self._warm_task
        self._warm_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            if self._ctx is not None:
                await self._ctx.close()
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        finally:
            self._ctx = self._browser = self._pw = None
            self._warm = False


async def search_catalog(
    query: str, *, cookie_path: Path, limit: int = 20, available_only: bool = False
) -> list[CatalogItem]:
    """One-shot convenience: warm a context, search, tear down. For a single
    query. Use a long-lived :class:`CatalogSearcher` for repeated searches."""
    searcher = CatalogSearcher(cookie_path=cookie_path)
    try:
        return await searcher.search(query, limit=limit, available_only=available_only)
    finally:
        await searcher.close()
