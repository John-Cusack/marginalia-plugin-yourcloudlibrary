"""Typed structures for YCL API responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LibraryInfo:
    """Per-library identity decoded from the ``__config_PROD`` cookie."""

    name: str
    url_name: str            # e.g. "PalmBeachCountyLibrarySystem"
    library_uuid: str        # e.g. "793edfa10e6743fc8ce5cf6b1b4147bf"
    reaktor_patron_id: int | None = None
    barcode: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class Book:
    """Subset of the detail-page Remix loader response we actually use."""

    item_id: str             # the user-visible book_id (e.g. "onc5689")
    isbn: str
    title: str
    status: str              # "LOAN" if currently borrowed
    can_read: bool
    page_count: int | None = None
    publisher: str | None = None
    language: str | None = None
    media_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReadingOrderItem:
    href: str                # relative path, e.g. "OEBPS/chapter01.xhtml"
    type: str                # MIME type, e.g. "application/xhtml+xml"


@dataclass(frozen=True)
class Manifest:
    """Subset of the Readium WebPub manifest we use."""

    book_uuid: str           # the path component in the content URL
    content_base_url: str    # https://epubservice.../content/{uuid}
    title: str
    isbn: str
    reading_order: list[ReadingOrderItem]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScrapeResult:
    """Output of ``scrape_book``."""

    book_id: str
    isbn: str
    title: str
    text: str
    chapter_count: int
    total_chars: int
