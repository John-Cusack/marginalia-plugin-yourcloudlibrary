"""Integration tier: the plugin against an installed ``marginalia-ai>=0.6``.

Run explicitly (``pytest -m integration``) in an environment with
``marginalia-ai==0.6.0`` installed and ``RE_DB_URL`` naming a Postgres server
the suite may create its scratch database on. Nothing here talks to
YourCloudLibrary; the book is a local fixture.
"""

from __future__ import annotations

import base64
import json

import pytest

from tests._core_fixtures import ingestion  # noqa: F401 — re-exported fixture
from ycl.api.types import Chapter, ScrapeResult

FIXTURE_LIBRARY = "FixtureLibrary"
FIXTURE_BOOK_ID = "fixture0001"


def fake_session_cookies() -> list[dict]:
    """Syntactically valid session cookies. They authenticate nothing."""
    config = {"library_info": {"name": "Fixture Library", "urlName": FIXTURE_LIBRARY},
              "login_info": {"library": "fixture"}}
    return [
        {"name": "__config_PROD", "value": base64.b64encode(json.dumps(config).encode()).decode(),
         "domain": ".yourcloudlibrary.com", "expires": -1},
        {"name": "__session_PROD", "value": "fixture-session", "domain": ".yourcloudlibrary.com",
         "expires": -1},
    ]


def fixture_book(book_id: str = FIXTURE_BOOK_ID) -> ScrapeResult:
    """A small original book: three chapters of generated prose."""

    def prose(topic: str) -> str:
        return " ".join(
            f"Paragraph {i} considers {topic} from angle number {i}, noting how the "
            f"argument develops and what the reader should carry forward."
            for i in range(1, 60)
        )

    chapters = [
        Chapter(index=0, href="OEBPS/c1.xhtml", title="On Libraries", text=prose("libraries")),
        Chapter(index=1, href="OEBPS/c2.xhtml", title="On Loans", text=prose("loans")),
        Chapter(index=2, href="OEBPS/c3.xhtml", title="On Returns", text=prose("returns")),
    ]
    return ScrapeResult(book_id=book_id, isbn="0000000000000", title="A Fixture Book",
                        chapters=chapters, author="Test, Author")


@pytest.fixture
def seeded_context(plugin_context, plugin_paths):
    """Plugin data dir holding fake cookies and a cached fixture book (no network needed)."""
    from ycl._textcache import write_text_cache
    from ycl.session.cookies import CookieStore

    CookieStore(plugin_paths.cookie_path).save(fake_session_cookies())
    book = fixture_book()
    write_text_cache(plugin_paths, FIXTURE_LIBRARY, book.book_id, book)
    return plugin_context, book
