"""Tests for the ycl.ingest_book handler — cache-path metadata + expiry.

Uses a fake ingestion client (captures the metadata passed to ingest_drafts),
a fake YclClient, and a tmp BorrowStore + extracted-text path. The
research_engine SDK (incl. ProseWindowChunker) is stubbed in tests/conftest.py.
"""

from __future__ import annotations

import pytest

import ycl.tools.ingest_book as mod
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


class _FakeIngestion:
    def __init__(self):
        self.metadata = None

    async def find_existing(self, source=None, source_pattern=None):
        return []

    async def ingest_drafts(self, *, title, document_type, passage_drafts, source, metadata):
        self.metadata = metadata
        return {"document_id": "doc-1", "passage_count": len(passage_drafts)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    extracted = tmp_path / "extracted"

    def _text_path(lib, bid):
        return extracted / lib / f"{bid}.txt"

    monkeypatch.setattr(mod, "text_path_for", _text_path)
    store = BorrowStore(tmp_path / "borrows.json")
    monkeypatch.setattr(mod, "BorrowStore", lambda: store)
    monkeypatch.setattr(
        mod.YclClient, "from_cookie_store", staticmethod(lambda *a, **k: _FakeClient())
    )

    def _seed_text(book_id="onc5689", text="Four Views\n\nBody."):
        path = _text_path(LIBRARY_KEY, book_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    return store, _seed_text


async def test_ingest_cache_path_carries_stored_author(env):
    store, seed_text = env
    seed_text()
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc5689",
        title="Four Views",
        author="Doe, Jane",
        subjects=["Ecclesiology"],
        description="<p>blurb</p>",
    )
    ingestion = _FakeIngestion()

    result = await mod.handler(book_id="onc5689", ingestion=ingestion)

    assert result["status"] == "ingested"
    assert ingestion.metadata["author"] == "Doe, Jane"
    assert ingestion.metadata["subjects"] == ["Ecclesiology"]
    assert ingestion.metadata["description"] == "<p>blurb</p>"
    assert result["author"] == "Doe, Jane"


async def test_ingest_preserves_authoritative_expiry(env):
    store, seed_text = env
    seed_text()
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc5689",
        title="Four Views",
        expires_at="2099-01-10T16:22:54Z",
        expires_at_is_estimated=False,
    )
    ingestion = _FakeIngestion()

    result = await mod.handler(book_id="onc5689", ingestion=ingestion)

    assert ingestion.metadata["expires_at"] == "2099-01-10T16:22:54Z"
    assert ingestion.metadata["expires_at_is_estimated"] is False
    assert result["expires_at"] == "2099-01-10T16:22:54Z"
    assert result["expires_at_is_estimated"] is False
    record = store.get(LIBRARY_KEY, "onc5689")
    assert record["expires_at"] == "2099-01-10T16:22:54Z"
    assert record["expires_at_is_estimated"] is False
