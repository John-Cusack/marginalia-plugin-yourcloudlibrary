"""Cookie-authenticated httpx client for the YCL backend.

This package replaces the Playwright-based reader scraper. Authentication
still requires a one-time browser login (via ``ycl.cli.login``), but every
subsequent operation is plain async httpx — no headless browser, no
page-turn loop.

The flow per book:
    1. GET ebook.../detail/{book_id}?_data=routes/library.$name.detail.$id
       → JSON with the book's ISBN, status, and canRead flag.
    2. GET epubservice.../manifest/{ISBN}?catalogName=3m.us
       → JSON string pointing at the actual manifest URL.
    3. GET that manifest URL
       → Readium WebPub manifest with readingOrder.
    4. GET each readingOrder item, base64-decode the body, parse XHTML, extract text.
"""

from .client import YclClient
from .errors import (
    AuthExpiredError,
    BookNotBorrowedError,
    NotAuthenticatedError,
    YclApiError,
)
from .scraper import scrape_book
from .types import Book, Chapter, Manifest, ReadingOrderItem, ScrapeResult

__all__ = [
    "AuthExpiredError",
    "Book",
    "BookNotBorrowedError",
    "Chapter",
    "Manifest",
    "NotAuthenticatedError",
    "ReadingOrderItem",
    "ScrapeResult",
    "YclApiError",
    "YclClient",
    "scrape_book",
]
