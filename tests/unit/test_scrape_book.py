"""Tests for the yourcloudlibrary.scrape_book handler — metadata persistence + expiry.

Exercised with a fake YclClient and a monkeypatched api_scrape_book (no
network); all state lands in the PluginContext's temp data directory.
"""

from __future__ import annotations

import pytest

import ycl.tools.scrape_book as mod
from ycl.api.types import Chapter, ScrapeResult
from ycl.borrows import BorrowStore

LIBRARY_KEY = "PalmBeachCountyLibrarySystem"


class _FakeLibrary:
    url_name = LIBRARY_KEY
    name = "Palm Beach County Library System"


class _FakeClient:
    def __init__(self):
        self.library = _FakeLibrary()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def close(self):
        pass


def _scrape_result(book_id="onc5689"):
    # ScrapeResult derives text/chapter_count/total_chars from its chapters.
    return ScrapeResult(
        book_id=book_id,
        isbn="9780310522744",
        title="Four Views",
        chapters=[
            Chapter(index=0, href="OEBPS/c1.xhtml", title="Ch1", text="Four Views"),
            Chapter(index=1, href="OEBPS/c2.xhtml", title="Ch2", text="Body text here."),
        ],
        author="Doe, Jane",
        subjects=["Ecclesiology", "Missions"],
        description="<p>blurb</p>",
    )


@pytest.fixture
def env(plugin_context, plugin_paths, monkeypatch):
    monkeypatch.setattr(mod, "acquire_client", lambda paths: (_FakeClient(), None))

    async def _fake_scrape(client, book_id, concurrency=4):
        return _scrape_result(book_id)

    monkeypatch.setattr(mod, "api_scrape_book", _fake_scrape)
    return plugin_context, BorrowStore(plugin_paths.borrows_path)


async def test_scrape_book_persists_author_subjects_description(env, plugin_paths):
    context, store = env
    result = await mod.handler(book_id="onc5689", context=context)

    assert result["status"] == "success"
    assert result["output_path"] == str(plugin_paths.text_path_for(LIBRARY_KEY, "onc5689"))
    record = store.get(LIBRARY_KEY, "onc5689")
    assert record["author"] == "Doe, Jane"
    assert record["subjects"] == ["Ecclesiology", "Missions"]
    assert record["description"] == "<p>blurb</p>"


async def test_scrape_book_preserves_authoritative_expiry(env):
    context, store = env
    # A prior sync wrote the real due date.
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc5689",
        expires_at="2099-01-10T16:22:54Z",
        expires_at_is_estimated=False,
    )

    result = await mod.handler(book_id="onc5689", context=context)  # no explicit expires_at

    assert result["expires_at"] == "2099-01-10T16:22:54Z"
    assert result["expires_at_is_estimated"] is False
    record = store.get(LIBRARY_KEY, "onc5689")
    assert record["expires_at"] == "2099-01-10T16:22:54Z"
    assert record["expires_at_is_estimated"] is False


async def test_scrape_book_estimates_when_no_stored_or_explicit_expiry(env):
    context, _ = env
    result = await mod.handler(book_id="onc5689", context=context)

    # Nothing authoritative on record → falls back to an estimate.
    assert result["expires_at"] is not None
    assert result["expires_at_is_estimated"] is True
