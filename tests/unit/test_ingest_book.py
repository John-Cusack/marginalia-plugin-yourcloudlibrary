"""yourcloudlibrary.ingest_book through the shared ingestion boundary (``ycl.tools._ingest``).

The fake ingestion client implements only the SDK surface the plugin may use —
``find_existing`` and ``ingest_document`` — so any regression to plugin-side
chunking (``ingest_drafts``) fails loudly. No network, no core.
"""

from __future__ import annotations

import pytest

import ycl.tools._ingest as boundary
import ycl.tools.ingest_book as mod
from ycl._paths import legacy_paths
from ycl._textcache import write_text_cache
from ycl.api.types import CHAPTER_SEPARATOR, Chapter, ScrapeResult
from ycl.borrows import BorrowStore

LIBRARY_KEY = "PalmBeachCountyLibrarySystem"


class _FakeLibrary:
    url_name = LIBRARY_KEY
    name = "Palm Beach County Library System"


class _FakeClient:
    library = _FakeLibrary()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def close(self):
        return None


class _FakeIngestion:
    """SDK IngestionClient double: records requests, remembers sources."""

    def __init__(self, existing: dict[str, dict] | None = None):
        self.requests: list[dict] = []
        self.existing = dict(existing or {})
        self.lookups: list[str] = []

    async def find_existing(self, *, source=None, source_pattern=None):
        self.lookups.append(source)
        doc = self.existing.get(source)
        return [doc] if doc else []

    async def ingest_document(
        self, *, title, document_type, text, source="", metadata=None, language=None,
        sections=None,
    ):
        self.requests.append(
            {"title": title, "document_type": document_type, "text": text, "source": source,
             "metadata": metadata, "sections": sections}
        )
        doc = {"document_id": f"doc-{len(self.requests)}", "passage_count": 3,
               "title": title, "source": source}
        self.existing[source] = doc
        return {"document_id": doc["document_id"], "passage_count": 3}

    @property
    def last(self) -> dict:
        return self.requests[-1]


@pytest.fixture
def env(plugin_context, plugin_paths, monkeypatch):
    monkeypatch.setattr(boundary, "acquire_client", lambda paths: (_FakeClient(), None))
    store = BorrowStore(plugin_paths.borrows_path)

    def write_cached_text(book_id: str, body: str) -> None:
        path = plugin_paths.text_path_for(LIBRARY_KEY, book_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    return plugin_context, plugin_paths, store, write_cached_text


def _chapters() -> list[Chapter]:
    return [
        Chapter(index=0, href="OEBPS/cover.xhtml", title=None, text="Cover."),
        Chapter(index=2, href="OEBPS/c1.xhtml", title="Chapter One", text="Body one. " * 40),
        Chapter(index=3, href="OEBPS/c2.xhtml", title="Chapter Two", text="Body two. " * 40),
    ]


async def test_requires_ingestion_client(env):
    context, *_ = env
    result = await mod.handler(book_id="onc5689", context=context)
    assert result["error_type"] == "config"


async def test_fresh_scrape_sends_canonical_text_and_chapter_sections(env, monkeypatch):
    context, paths, store, _ = env
    scraped = ScrapeResult(
        book_id="onc5689", isbn="9780310522744", title="Four Views", chapters=_chapters(),
        author="Doe, Jane", subjects=["Ecclesiology"],
    )

    async def fake_scrape(client, book_id, concurrency=4):
        return scraped

    monkeypatch.setattr(boundary, "api_scrape_book", fake_scrape)
    ingestion = _FakeIngestion()

    result = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert result["status"] == "ingested"
    request = ingestion.last
    assert request["document_type"] == "ycl_book"
    assert request["text"] == scraped.text
    assert request["source"] == str(paths.text_path_for(LIBRARY_KEY, "onc5689").resolve())
    # One node per chapter, each span addressing exactly that chapter's prose.
    sections = request["sections"]
    assert [s["heading"] for s in sections] == [None, "Chapter One", "Chapter Two"]
    assert [s["chapter_index"] for s in sections] == [0, 2, 3]
    for section, chapter in zip(sections, scraped.chapters, strict=True):
        assert request["text"][section["char_start"] : section["char_end"]] == chapter.text
    # Cache and registry written under the context's data directory.
    assert paths.text_path_for(LIBRARY_KEY, "onc5689").read_text() == scraped.text
    assert store.get(LIBRARY_KEY, "onc5689")["document_id"] == "doc-1"


async def test_metadata_is_document_level_and_complete(env, monkeypatch):
    context, _, _, write_cached_text = env
    write_cached_text("onc5689", "Some text.")
    ingestion = _FakeIngestion()

    await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert set(ingestion.last["metadata"]) == {
        "library_id", "library_name", "book_id", "ycl_title", "author", "subjects",
        "description", "isbn", "borrowed_at", "expires_at", "expires_at_is_estimated",
        "scraped_at", "char_count", "chapter_count", "partial_scrape", "source_url",
    }
    assert ingestion.last["metadata"]["source_url"] == (
        "https://epub.yourcloudlibrary.com/read/onc5689"
    )
    assert ingestion.last["sections"] is None  # no sidecar → flat document


async def test_repeat_returns_existing_document(env):
    context, _, _, write_cached_text = env
    write_cached_text("onc5689", "Some text.")
    ingestion = _FakeIngestion()

    first = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)
    second = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert first["status"] == "ingested"
    assert second["status"] == "already_ingested"
    assert second["document_id"] == first["document_id"]
    assert len(ingestion.requests) == 1


async def test_book_ingested_under_legacy_path_is_not_duplicated(env):
    context, _, _, write_cached_text = env
    write_cached_text("onc5689", "Some text.")
    legacy_source = str(legacy_paths().text_path_for(LIBRARY_KEY, "onc5689").resolve())
    ingestion = _FakeIngestion(
        {legacy_source: {"document_id": "old-doc", "title": "T", "source": legacy_source}}
    )

    result = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert result["status"] == "already_ingested"
    assert result["document_id"] == "old-doc"
    assert ingestion.requests == []


async def test_force_reingest_skips_lookup(env):
    context, _, _, write_cached_text = env
    write_cached_text("onc5689", "Some text.")
    ingestion = _FakeIngestion()
    await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    result = await mod.handler(
        book_id="onc5689", force_reingest=True, ingestion=ingestion, context=context
    )
    assert result["status"] == "ingested"
    assert len(ingestion.requests) == 2


# --- cached re-ingest (P2.4) ------------------------------------------------


async def test_cached_ingest_reads_title_from_borrowstore(env):
    context, _, store, write_cached_text = env
    # Cached text whose first line is cover junk — never used as the title.
    write_cached_text("onc5689", "COVER IMAGE\n\nReal opening sentence of the book.")
    store.upsert(
        library_id=LIBRARY_KEY, book_id="onc5689", title="The Real Recorded Title",
        isbn="9780310522744", chapter_count=12,
    )
    ingestion = _FakeIngestion()

    result = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert result["title"] == "The Real Recorded Title"
    assert ingestion.last["title"] == "The Real Recorded Title"


async def test_cached_ingest_explicit_title_overrides_store(env):
    context, _, store, write_cached_text = env
    write_cached_text("onc5689", "COVER IMAGE\n\nBody text.")
    store.upsert(library_id=LIBRARY_KEY, book_id="onc5689", title="Stored Title")

    result = await mod.handler(
        book_id="onc5689", title="Caller Override", ingestion=_FakeIngestion(), context=context
    )
    assert result["title"] == "Caller Override"


async def test_cached_ingest_recovers_chapter_sections_and_title_from_sidecar(env):
    context, paths, store, _ = env
    chapters = _chapters()
    scraped = ScrapeResult(
        book_id="onc5689", isbn="9780310522744", title="Real Book Title", chapters=chapters
    )
    write_text_cache(paths, LIBRARY_KEY, "onc5689", scraped)
    store.upsert(library_id=LIBRARY_KEY, book_id="onc5689")  # no recorded title
    ingestion = _FakeIngestion()

    result = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert result["title"] == "Real Book Title"
    sections = ingestion.last["sections"]
    assert [s["heading"] for s in sections] == [None, "Chapter One", "Chapter Two"]
    text = ingestion.last["text"]
    assert [text[s["char_start"] : s["char_end"]] for s in sections] == [c.text for c in chapters]


# --- P1: cache-path metadata + authoritative-expiry preservation -----------


async def test_ingest_cache_path_carries_stored_author(env):
    context, _, store, write_cached_text = env
    write_cached_text("onc5689", "Four Views\n\nBody.")
    store.upsert(
        library_id=LIBRARY_KEY, book_id="onc5689", title="Four Views", author="Doe, Jane",
        subjects=["Ecclesiology"], description="<p>blurb</p>",
    )
    ingestion = _FakeIngestion()

    result = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert result["status"] == "ingested"
    assert ingestion.last["metadata"]["author"] == "Doe, Jane"
    assert ingestion.last["metadata"]["subjects"] == ["Ecclesiology"]
    assert ingestion.last["metadata"]["description"] == "<p>blurb</p>"
    assert result["author"] == "Doe, Jane"


async def test_ingest_preserves_authoritative_expiry(env):
    context, _, store, write_cached_text = env
    write_cached_text("onc5689", "Four Views\n\nBody.")
    store.upsert(
        library_id=LIBRARY_KEY, book_id="onc5689", title="Four Views",
        expires_at="2099-01-10T16:22:54Z", expires_at_is_estimated=False,
    )
    ingestion = _FakeIngestion()

    result = await mod.handler(book_id="onc5689", ingestion=ingestion, context=context)

    assert ingestion.last["metadata"]["expires_at"] == "2099-01-10T16:22:54Z"
    assert ingestion.last["metadata"]["expires_at_is_estimated"] is False
    assert result["expires_at"] == "2099-01-10T16:22:54Z"
    record = store.get(LIBRARY_KEY, "onc5689")
    assert record["expires_at"] == "2099-01-10T16:22:54Z"
    assert record["expires_at_is_estimated"] is False


# --- chapter_sections --------------------------------------------------------


def test_chapter_sections_rejects_chapters_that_do_not_tile_the_text():
    chapters = _chapters()
    text = CHAPTER_SEPARATOR.join(c.text for c in chapters)
    assert boundary.chapter_sections(text, chapters)
    assert boundary.chapter_sections("different " + text, chapters) == []
