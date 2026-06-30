"""Tests for the ycl.ingest_book handler.

Covers two areas merged from the P1 and P2 work:
  * P1 — cache-path metadata (author/subjects/description) + authoritative
    expiry preservation. These run against the stubbed research_engine SDK
    (incl. a trivial ProseWindowChunker) from tests/conftest.py.
  * P2 — chapter-aware chunking (P2.5) and the cached-title fix (P2.4). The
    chunking assertions need the *real* ProseWindowChunker (passage objects
    with .position/.metadata), so they skip when only the stub is available.
"""

from __future__ import annotations

import pytest

import ycl._paths as paths
import ycl.tools.ingest_book as mod
from ycl.api.types import Chapter, ScrapeResult
from ycl.borrows import BorrowStore

LIBRARY_KEY = "PalmBeachCountyLibrarySystem"

# The stub ProseWindowChunker (conftest) takes no kwargs and yields plain
# dicts; the real one accepts window sizes and yields passage objects with
# .position/.metadata. Detect which we have so chunk-structure tests only run
# against the real implementation (inside the Marginalia harness).
from research_engine.services.ingestion.chunking.prose_window import (  # noqa: E402
    ProseWindowChunker,
)

try:
    ProseWindowChunker(max_tokens=8, overlap_tokens=1)
    _HAS_REAL_CHUNKER = True
except TypeError:
    _HAS_REAL_CHUNKER = False

needs_real_chunker = pytest.mark.skipif(
    not _HAS_REAL_CHUNKER, reason="needs the real ProseWindowChunker (harness only)"
)

# Long enough that each chapter chunks into several passages, so the global
# position renumbering across chapters is actually exercised.
_LONG = "This is a sentence about libraries. " * 60


class _FakeLibrary:
    url_name = LIBRARY_KEY
    name = "Palm Beach County Library System"


class _FakeClient:
    library = _FakeLibrary()

    @classmethod
    def from_cookie_store(cls, *_a, **_k):
        return cls()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def close(self):
        return None


class _FakeIngestion:
    def __init__(self):
        self.metadata = None
        self.ingested_title = None
        self.drafts: list = []

    async def find_existing(self, source=None, **_):
        return []

    async def ingest_drafts(self, *, title, document_type, passage_drafts, source, metadata):
        self.ingested_title = title
        self.drafts = passage_drafts
        self.metadata = metadata
        return {"document_id": "doc-1", "passage_count": len(passage_drafts)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect the extracted-text dir + store + client into tmp_path.

    Patching ``_paths.EXTRACTED_DIR`` (read dynamically by ``text_path_for`` /
    ``chapters_path_for``) redirects both the handler's cache and the chapter
    sidecar in one shot.
    """
    monkeypatch.setattr(paths, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(mod, "YclClient", _FakeClient)
    store = BorrowStore(path=tmp_path / "borrows.json")
    monkeypatch.setattr(mod, "BorrowStore", lambda: store)

    def _write_cached_text(book_id: str, body: str) -> None:
        path = paths.text_path_for(LIBRARY_KEY, book_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    return store, _write_cached_text


# --- P2.5: per-chapter chunking carries chapter location -------------------


@needs_real_chunker
async def test_chunk_with_chapters_attaches_titles_and_renumbers():
    chunker = ProseWindowChunker(max_tokens=80, overlap_tokens=10)
    chapters = [
        Chapter(index=0, href="OEBPS/c1.xhtml", title="Chapter One", text=_LONG),
        Chapter(index=1, href="OEBPS/c2.xhtml", title="Chapter Two", text=_LONG),
    ]
    base_meta = {"book_id": "onc5689", "library_id": "lib"}

    drafts = await mod._chunk_with_chapters(chunker, "ignored", chapters, base_meta)

    assert len(drafts) > 2
    assert [d.position for d in drafts] == list(range(len(drafts)))
    titles = {d.metadata["chapter_title"] for d in drafts}
    assert titles == {"Chapter One", "Chapter Two"}
    assert all(d.metadata["book_id"] == "onc5689" for d in drafts)
    first = next(d for d in drafts if d.metadata["chapter_title"] == "Chapter One")
    assert first.metadata["chapter_index"] == 0


@needs_real_chunker
async def test_chunk_with_chapters_falls_back_without_structure():
    chunker = ProseWindowChunker()
    drafts = await mod._chunk_with_chapters(chunker, _LONG, [], {"book_id": "onc5689"})

    assert drafts  # whole-text path still produces passages
    assert all("chapter_title" not in d.metadata for d in drafts)


# --- P2.4: cached re-ingest uses the BorrowStore title, not text sniffing --


async def test_cached_ingest_reads_title_from_borrowstore(env):
    store, write_cached_text = env
    book_id = "onc5689"
    # Cached text whose first line is cover junk — the old code would have
    # used "COVER IMAGE" as the title.
    write_cached_text(book_id, "COVER IMAGE\n\nReal opening sentence of the book.")
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id=book_id,
        title="The Real Recorded Title",
        isbn="9780310522744",
        chapter_count=12,
    )

    fake_ingestion = _FakeIngestion()
    result = await mod.handler(book_id=book_id, ingestion=fake_ingestion)

    assert result["status"] == "ingested"
    assert result["title"] == "The Real Recorded Title"
    assert fake_ingestion.ingested_title == "The Real Recorded Title"
    assert "COVER IMAGE" not in result["title"]


async def test_cached_ingest_explicit_title_overrides_store(env):
    store, write_cached_text = env
    book_id = "onc5689"
    write_cached_text(book_id, "COVER IMAGE\n\nBody text.")
    store.upsert(library_id=LIBRARY_KEY, book_id=book_id, title="Stored Title")

    result = await mod.handler(
        book_id=book_id, title="Caller Override", ingestion=_FakeIngestion()
    )
    assert result["title"] == "Caller Override"


@needs_real_chunker
async def test_cached_ingest_recovers_chapters_and_title_from_sidecar(env):
    """A prior scrape wrote text + chapter sidecar; re-ingesting from cache must
    rebuild chapter metadata and recover the title even when BorrowStore has no
    recorded title."""
    from ycl._textcache import write_text_cache

    store, _ = env
    book_id = "onc5689"
    chapters = [
        Chapter(index=0, href="OEBPS/c1.xhtml", title="Chapter One", text="Body one. " * 40),
        Chapter(index=1, href="OEBPS/c2.xhtml", title="Chapter Two", text="Body two. " * 40),
    ]
    write_text_cache(
        LIBRARY_KEY,
        book_id,
        ScrapeResult(
            book_id=book_id, isbn="9780310522744", title="Real Book Title", chapters=chapters
        ),
    )
    # BorrowStore record exists but has NO title.
    store.upsert(library_id=LIBRARY_KEY, book_id=book_id)

    fake_ingestion = _FakeIngestion()
    result = await mod.handler(book_id=book_id, ingestion=fake_ingestion)

    assert result["title"] == "Real Book Title"  # recovered from sidecar
    chapter_titles = {d.metadata.get("chapter_title") for d in fake_ingestion.drafts}
    assert chapter_titles == {"Chapter One", "Chapter Two"}
    assert [d.position for d in fake_ingestion.drafts] == list(
        range(len(fake_ingestion.drafts))
    )


# --- P1: cache-path metadata + authoritative-expiry preservation -----------


async def test_ingest_cache_path_carries_stored_author(env):
    store, write_cached_text = env
    write_cached_text("onc5689", "Four Views\n\nBody.")
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
    store, write_cached_text = env
    write_cached_text("onc5689", "Four Views\n\nBody.")
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
