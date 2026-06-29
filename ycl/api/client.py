"""Async httpx client for the YCL backend.

Handles cookie loading, the 4-step manifest flow, and chapter fetching.
Translates HTTP errors to the typed errors in :mod:`ycl.api.errors` so
callers can react to "needs re-login" vs "book not borrowed" vs "transient
network issue" cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import structlog

from .._paths import COOKIE_PATH
from ..session.cookies import CookieStore
from .cookies import cookies_to_jar, decode_config_cookie
from .errors import AuthExpiredError, BookNotBorrowedError, NotAuthenticatedError, YclApiError
from .types import Book, LibraryInfo, Loan, Manifest, ReadingOrderItem

log = structlog.get_logger(__name__)

DEFAULT_CATALOG_NAME = "3m.us"
EBOOK_HOST = "https://ebook.yourcloudlibrary.com"
EPUBSERVICE_HOST = "https://epubservice.yourcloudlibrary.com"
EPUB_ORIGIN = "https://epub.yourcloudlibrary.com"

# The active-loans response comes from the My-Books page's ``.current`` child
# route, which YCL implements as a Remix *action* (POST), not a `_data` loader
# GET. Confirmed live 2026-06-29; see scripts/probe_loans.py and IMPL_NOTES.md.
DETAIL_ROUTE = "routes/library.$name.detail.$id"
LOANS_ROUTE = "routes/library.$name.mybooks.current"
LOANS_PAGE_SIZE = 20


class YclClient:
    """Cookie-authenticated httpx client for the YCL API surface.

    Construct via :py:meth:`from_cookie_store` so the cookie file is the
    single source of truth. The same client instance is intended to be
    reused for the lifetime of a tool invocation.
    """

    def __init__(
        self,
        *,
        cookie_jar: dict[str, str],
        library_info: LibraryInfo,
        catalog_name: str = DEFAULT_CATALOG_NAME,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._jar = cookie_jar
        self.library = library_info
        self.catalog_name = catalog_name
        self._client = httpx.AsyncClient(
            cookies=cookie_jar,
            follow_redirects=True,
            timeout=timeout_seconds,
            headers={
                "Origin": EPUB_ORIGIN,
                "Referer": f"{EPUB_ORIGIN}/",
                "Accept": "application/json, text/plain, */*",
            },
        )

    @classmethod
    def from_cookie_store(
        cls,
        path: Path = COOKIE_PATH,
        *,
        catalog_name: str = DEFAULT_CATALOG_NAME,
    ) -> YclClient:
        store = CookieStore(path)
        cookies = store.load()
        if not cookies:
            raise NotAuthenticatedError(
                f"No cookie file at {path}. Run `python -m ycl.cli.login` once."
            )
        library = decode_config_cookie(cookies)
        jar = cookies_to_jar(cookies)
        return cls(cookie_jar=jar, library_info=library, catalog_name=catalog_name)

    async def __aenter__(self) -> YclClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ----- core flow -------------------------------------------------------

    async def get_book(self, book_id: str) -> Book:
        """Fetch the detail-page Remix loader for ``book_id``.

        Returns a :class:`Book` even if the loan is no longer active; check
        ``book.status`` and ``book.can_read`` before scraping.
        """
        slug = self.library.url_name
        if not slug:
            raise YclApiError("library url_name unknown — cookie may be malformed")
        url = f"{EBOOK_HOST}/library/{slug}/detail/{book_id}"
        params = {"_data": DETAIL_ROUTE}
        resp = await self._get(url, params=params)
        data = resp.json()
        book_raw = data.get("book") or {}
        if not book_raw:
            raise YclApiError(
                f"detail loader returned no book object for book_id={book_id!r}"
            )
        return Book(
            item_id=book_raw.get("itemId") or book_id,
            isbn=str(book_raw.get("isbn") or ""),
            title=book_raw.get("title") or "Untitled",
            status=str(book_raw.get("status") or ""),
            can_read=bool(book_raw.get("canRead") in (True, "True", "true")),
            page_count=_safe_int(book_raw.get("page")),
            publisher=book_raw.get("publisher"),
            language=book_raw.get("language"),
            media_type=book_raw.get("mediaType"),
            author=_extract_author(book_raw),
            subjects=_extract_subjects(book_raw.get("contentCategories")),
            description=book_raw.get("description"),
            raw=book_raw,
        )

    async def get_loans(
        self,
        *,
        page_size: int = LOANS_PAGE_SIZE,
        sort: str = "BorrowedDateDescending",
    ) -> list[Loan]:
        """Fetch the patron's currently-active loans.

        Walks every segment of the paginated ``mybooks.current`` action and
        returns one :class:`Loan` per active loan. ``loan.due_date`` is the
        authoritative expiration the caller should persist (no estimation).
        """
        slug = self.library.url_name
        if not slug:
            raise YclApiError("library url_name unknown — cookie may be malformed")
        url = f"{EBOOK_HOST}/library/{slug}/mybooks/current"
        loans: list[Loan] = []
        segment = 1
        while True:
            params = {"segment": segment, "pageSize": page_size, "_data": LOANS_ROUTE}
            resp = await self._post(url, params=params, data={"format": "", "sort": sort})
            body = resp.json()
            for item in body.get("patronItems") or []:
                loans.append(_loan_from_item(item))
            total_segments = _safe_int(body.get("totalSegments")) or 1
            if segment >= total_segments:
                break
            segment += 1
        return loans

    async def get_manifest(self, isbn: str) -> Manifest:
        """Resolve the Readium WebPub manifest for ``isbn``."""
        if not isbn:
            raise YclApiError("missing ISBN")
        # Step 1: lookup endpoint returns a JSON-encoded URL string.
        lookup_url = f"{EPUBSERVICE_HOST}/manifest/{isbn}"
        resp = await self._get(lookup_url, params={"catalogName": self.catalog_name})
        manifest_url = _coerce_url_payload(resp.text)
        # Step 2: fetch the manifest itself.
        resp = await self._get(manifest_url)
        body = json.loads(resp.text)

        reading_order = [
            ReadingOrderItem(href=item["href"], type=item.get("type", ""))
            for item in body.get("readingOrder") or []
        ]
        if not reading_order:
            raise YclApiError(f"manifest for ISBN={isbn!r} had empty readingOrder")
        # The content base URL is everything up to the last '/' of the manifest URL.
        base = manifest_url.rsplit("/", 1)[0]
        # The book uuid is the last path component before /manifest.json.
        book_uuid = base.rsplit("/", 1)[-1]
        return Manifest(
            book_uuid=book_uuid,
            content_base_url=base,
            title=(body.get("metadata") or {}).get("title") or "",
            isbn=isbn,
            reading_order=reading_order,
            raw=body,
        )

    async def fetch_chapter_text(
        self, manifest: Manifest, item: ReadingOrderItem
    ) -> str:
        """Fetch ``item.href`` and return the decoded base64 body as raw XHTML."""
        from .text import decode_chapter_body  # local import: avoid cycle

        url = f"{manifest.content_base_url}/{item.href.lstrip('/')}"
        resp = await self._get(url)
        return decode_chapter_body(resp.text)

    # ----- internals -------------------------------------------------------

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise YclApiError(f"network error on GET {url}: {exc}") from exc
        return self._check("GET", url, resp)

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise YclApiError(f"network error on POST {url}: {exc}") from exc
        return self._check("POST", url, resp)

    @staticmethod
    def _check(method: str, url: str, resp: httpx.Response) -> httpx.Response:
        if resp.status_code in (401, 403):
            raise AuthExpiredError(
                f"{method} {url} returned {resp.status_code}; re-run ycl.cli.login."
            )
        # The unauth bounce: server says 200 but we landed on the marketing
        # page. Treat as auth-expired so the caller can prompt for re-login.
        if "yourcloudlibrary.com/en/home" in str(resp.url):
            raise AuthExpiredError(
                f"{method} {url} bounced to marketing — session likely expired."
            )
        if resp.status_code >= 400:
            raise YclApiError(
                f"{method} {url} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_author(book_raw: dict[str, Any]) -> str | None:
    """Pull a human-readable author string from a detail/loan payload.

    The detail loader exposes authors under ``contributors`` (a list of
    ``{"name": ...}`` dicts, where a single entry may itself be a
    ``"Last, First; Last, First"`` string). Loan payloads expose a flat
    ``author`` string. Accept either; return ``None`` if neither is present.
    """
    contributors = book_raw.get("contributors")
    if isinstance(contributors, list):
        names = [
            str(c.get("name")).strip()
            for c in contributors
            if isinstance(c, dict) and c.get("name")
        ]
        joined = "; ".join(n for n in names if n).strip().rstrip(";, ").strip()
        if joined:
            return joined
    author = book_raw.get("author")
    if isinstance(author, str) and author.strip():
        return author.strip()
    return None


def _extract_subjects(content_categories: Any) -> list[str]:
    """Flatten ``contentCategories`` into a de-duplicated list of subject names.

    ``contentCategories`` is a dict keyed by opaque category id; each value
    carries a human-readable ``name`` (e.g. "Ecclesiology", "Missions"). We
    keep insertion order and drop blanks/dupes. Returns ``[]`` for anything
    unexpected so callers never have to guard the shape.
    """
    if not isinstance(content_categories, dict):
        return []
    subjects: list[str] = []
    for entry in content_categories.values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.strip() and name not in subjects:
            subjects.append(name.strip())
    return subjects


def _loan_from_item(item: dict[str, Any]) -> Loan:
    """Build a :class:`Loan` from one ``patronItems`` entry."""
    return Loan(
        item_id=str(item.get("itemId") or ""),
        title=item.get("title") or "Untitled",
        due_date=str(item.get("dueDate") or ""),
        loan_id=item.get("loanId"),
        media_type=item.get("mediaType"),
        author=_extract_author(item),
        can_renew=bool(item.get("canRenew")),
        can_return=bool(item.get("canReturn")),
        raw=item,
    )


def _coerce_url_payload(body: str) -> str:
    """The ``/manifest/{ISBN}`` endpoint returns either a JSON string or a
    bare URL — accept both and normalize to the unquoted URL."""
    s = body.strip()
    if s.startswith('"') and s.endswith('"'):
        return json.loads(s)
    return s


def assert_borrowed(book: Book) -> None:
    """Raise :class:`BookNotBorrowedError` if ``book`` is not currently readable."""
    if not book.can_read or book.status != "LOAN":
        raise BookNotBorrowedError(book.item_id, book.status)
