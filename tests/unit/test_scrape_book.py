"""Tests for the ycl.scrape_book handler — metadata persistence + expiry.

Exercised with a fake YclClient and a monkeypatched api_scrape_book (no
network), and with on-disk paths + BorrowStore redirected to a tmp dir.
"""

from __future__ import annotations

import pytest

import ycl.tools.scrape_book as mod
from ycl.api.types import ScrapeResult
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
    return ScrapeResult(
        book_id=book_id,
        isbn="9780310522744",
        title="Four Views",
        text="Four Views\n\nBody text here.",
        chapter_count=2,
        total_chars=27,
        author="Doe, Jane",
        subjects=["Ecclesiology", "Missions"],
        description="<p>blurb</p>",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    extracted = tmp_path / "extracted"
    monkeypatch.setattr(mod, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(
        mod, "text_path_for", lambda lib, bid: extracted / lib / f"{bid}.txt"
    )
    store = BorrowStore(tmp_path / "borrows.json")
    monkeypatch.setattr(mod, "BorrowStore", lambda: store)
    monkeypatch.setattr(
        mod.YclClient, "from_cookie_store", staticmethod(lambda *a, **k: _FakeClient())
    )

    async def _fake_scrape(client, book_id, concurrency=4):
        return _scrape_result(book_id)

    monkeypatch.setattr(mod, "api_scrape_book", _fake_scrape)
    return store


async def test_scrape_book_persists_author_subjects_description(env):
    result = await mod.handler(book_id="onc5689")

    assert result["status"] == "success"
    record = env.get(LIBRARY_KEY, "onc5689")
    assert record["author"] == "Doe, Jane"
    assert record["subjects"] == ["Ecclesiology", "Missions"]
    assert record["description"] == "<p>blurb</p>"


async def test_scrape_book_preserves_authoritative_expiry(env):
    # A prior sync wrote the real due date.
    env.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc5689",
        expires_at="2099-01-10T16:22:54Z",
        expires_at_is_estimated=False,
    )

    result = await mod.handler(book_id="onc5689")  # no explicit expires_at

    assert result["expires_at"] == "2099-01-10T16:22:54Z"
    assert result["expires_at_is_estimated"] is False
    record = env.get(LIBRARY_KEY, "onc5689")
    assert record["expires_at"] == "2099-01-10T16:22:54Z"
    assert record["expires_at_is_estimated"] is False


async def test_scrape_book_estimates_when_no_stored_or_explicit_expiry(env):
    result = await mod.handler(book_id="onc5689")

    # Nothing authoritative on record → falls back to an estimate.
    assert result["expires_at"] is not None
    assert result["expires_at_is_estimated"] is True
