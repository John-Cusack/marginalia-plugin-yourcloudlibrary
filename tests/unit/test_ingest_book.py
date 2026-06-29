"""ycl.ingest_book — chapter-aware chunking (P2.5) and cached-title fix (P2.4).

These exercise the tool layer, which imports ``research_engine``. That package
(and its transitive deps) is only present inside the Marginalia harness, not in
the plugin's standalone unit-test venv — so the whole module is skipped when it
isn't importable, mirroring how the rest of the suite stays network/harness
free. The pure-API layers (cookies, client, scraper) are covered unconditionally
elsewhere.
"""

from __future__ import annotations

import pytest

from ycl.api.types import Chapter

# Skip cleanly when the harness runtime isn't installed.
ib = pytest.importorskip("ycl.tools.ingest_book")
_prose = pytest.importorskip(
    "research_engine.services.ingestion.chunking.prose_window"
)
from ycl.borrows import BorrowStore  # noqa: E402  (after importorskip guard)

ProseWindowChunker = _prose.ProseWindowChunker

# Long enough that each chapter chunks into several passages, so the global
# position renumbering across chapters is actually exercised.
_LONG = "This is a sentence about libraries. " * 60


# --- P2.5: per-chapter chunking carries chapter location -----------------


async def test_chunk_with_chapters_attaches_titles_and_renumbers():
    chunker = ProseWindowChunker(max_tokens=80, overlap_tokens=10)
    chapters = [
        Chapter(index=0, href="OEBPS/c1.xhtml", title="Chapter One", text=_LONG),
        Chapter(index=1, href="OEBPS/c2.xhtml", title="Chapter Two", text=_LONG),
    ]
    base_meta = {"book_id": "onc5689", "library_id": "lib"}

    drafts = await ib._chunk_with_chapters(chunker, "ignored", chapters, base_meta)

    # More than one chapter's worth, and positions are a single 0..N-1 run.
    assert len(drafts) > 2
    assert [d.position for d in drafts] == list(range(len(drafts)))
    # Base metadata is preserved and chapter location is attached.
    titles = {d.metadata["chapter_title"] for d in drafts}
    assert titles == {"Chapter One", "Chapter Two"}
    assert all(d.metadata["book_id"] == "onc5689" for d in drafts)
    first = next(d for d in drafts if d.metadata["chapter_title"] == "Chapter One")
    assert first.metadata["chapter_index"] == 0


async def test_chunk_with_chapters_falls_back_without_structure():
    chunker = ProseWindowChunker()
    drafts = await ib._chunk_with_chapters(chunker, _LONG, [], {"book_id": "onc5689"})

    assert drafts  # whole-text path still produces passages
    assert all("chapter_title" not in d.metadata for d in drafts)


# --- P2.4: cached re-ingest uses the BorrowStore title, not text sniffing -


class _FakeLibrary:
    url_name = "PalmBeachCountyLibrarySystem"
    name = "Palm Beach County Library System"


class _FakeClient:
    library = _FakeLibrary()

    @classmethod
    def from_cookie_store(cls):
        return cls()

    async def close(self):
        return None


class _FakeIngestion:
    def __init__(self):
        self.ingested_title: str | None = None
        self.drafts: list = []

    async def find_existing(self, source=None, **_):
        return []

    async def ingest_drafts(self, *, title, document_type, passage_drafts, **_):
        self.ingested_title = title
        self.drafts = passage_drafts
        return {"document_id": "doc-1", "passage_count": len(passage_drafts)}


_LIBRARY = "PalmBeachCountyLibrarySystem"


def _patch_paths(monkeypatch, tmp_path):
    """Redirect the extracted-text dir + store/client into tmp_path."""
    import ycl._paths as paths

    monkeypatch.setattr(paths, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(ib, "YclClient", _FakeClient)
    monkeypatch.setattr(
        ib, "BorrowStore", lambda: BorrowStore(path=tmp_path / "borrows.json")
    )


def _write_cached_text(book_id: str, body: str) -> None:
    from ycl._paths import text_path_for

    path = text_path_for(_LIBRARY, book_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


async def test_cached_ingest_reads_title_from_borrowstore(tmp_path, monkeypatch):
    book_id = "onc5689"
    _patch_paths(monkeypatch, tmp_path)
    # Cached text whose first line is cover junk — the old code would have
    # used "COVER IMAGE" as the title.
    _write_cached_text(book_id, "COVER IMAGE\n\nReal opening sentence of the book.")

    BorrowStore(path=tmp_path / "borrows.json").upsert(
        library_id=_LIBRARY,
        book_id=book_id,
        title="The Real Recorded Title",
        isbn="9780310522744",
        chapter_count=12,
    )

    fake_ingestion = _FakeIngestion()
    result = await ib.handler(book_id=book_id, ingestion=fake_ingestion)

    assert result["status"] == "ingested"
    assert result["title"] == "The Real Recorded Title"
    assert fake_ingestion.ingested_title == "The Real Recorded Title"
    assert "COVER IMAGE" not in result["title"]


async def test_cached_ingest_explicit_title_overrides_store(tmp_path, monkeypatch):
    book_id = "onc5689"
    _patch_paths(monkeypatch, tmp_path)
    _write_cached_text(book_id, "COVER IMAGE\n\nBody text.")
    BorrowStore(path=tmp_path / "borrows.json").upsert(
        library_id=_LIBRARY, book_id=book_id, title="Stored Title"
    )

    result = await ib.handler(
        book_id=book_id, title="Caller Override", ingestion=_FakeIngestion()
    )
    assert result["title"] == "Caller Override"


async def test_cached_ingest_recovers_chapters_and_title_from_sidecar(
    tmp_path, monkeypatch
):
    """A prior scrape wrote text + chapter sidecar; re-ingesting from cache must
    rebuild chapter metadata and recover the title even when BorrowStore has
    no recorded title."""
    from ycl._textcache import write_text_cache
    from ycl.api.types import Chapter, ScrapeResult

    book_id = "onc5689"
    _patch_paths(monkeypatch, tmp_path)

    chapters = [
        Chapter(index=0, href="OEBPS/c1.xhtml", title="Chapter One", text="Body one. " * 40),
        Chapter(index=1, href="OEBPS/c2.xhtml", title="Chapter Two", text="Body two. " * 40),
    ]
    write_text_cache(
        _LIBRARY,
        book_id,
        ScrapeResult(book_id=book_id, isbn="9780310522744", title="Real Book Title", chapters=chapters),
    )
    # BorrowStore record exists but has NO title.
    BorrowStore(path=tmp_path / "borrows.json").upsert(library_id=_LIBRARY, book_id=book_id)

    fake_ingestion = _FakeIngestion()
    result = await ib.handler(book_id=book_id, ingestion=fake_ingestion)

    assert result["title"] == "Real Book Title"  # recovered from sidecar
    chapter_titles = {d.metadata.get("chapter_title") for d in fake_ingestion.drafts}
    assert chapter_titles == {"Chapter One", "Chapter Two"}
    # Positions are a single monotonic run across the rebuilt chapters.
    assert [d.position for d in fake_ingestion.drafts] == list(
        range(len(fake_ingestion.drafts))
    )
