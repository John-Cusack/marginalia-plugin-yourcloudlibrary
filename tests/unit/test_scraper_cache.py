"""Scraper chapter helpers + on-disk chapter-structure cache sidecar.

Both layers are pure ycl.api / ycl._textcache code (no research_engine), so
they run in the standalone unit-test venv.
"""

from __future__ import annotations

from ycl._textcache import read_chapter_sidecar, write_text_cache
from ycl.api.scraper import _href_key, chapter_specs, chapters_from_specs
from ycl.api.types import CHAPTER_SEPARATOR, Chapter, ScrapeResult


def test_href_key_strips_fragment_and_leading_slash():
    assert _href_key("/OEBPS/ch01.xhtml#sec2") == "OEBPS/ch01.xhtml"
    assert _href_key("OEBPS/ch01.xhtml") == "OEBPS/ch01.xhtml"


def _chapters() -> list[Chapter]:
    return [
        Chapter(index=0, href="OEBPS/cover.xhtml", title=None, text="Cover art here."),
        Chapter(index=1, href="OEBPS/c1.xhtml", title="Chapter One", text="Real body.\n\nMore."),
        Chapter(index=2, href="OEBPS/c2.xhtml", title="Chapter Two", text="Second chapter."),
    ]


def test_specs_roundtrip_reconstructs_chapters_exactly():
    chapters = _chapters()
    text = CHAPTER_SEPARATOR.join(c.text for c in chapters)

    rebuilt = chapters_from_specs(text, chapter_specs(chapters))

    assert len(rebuilt) == len(chapters)
    for original, got in zip(chapters, rebuilt, strict=True):
        assert got.index == original.index
        assert got.href == original.href
        assert got.title == original.title
        assert got.text == original.text  # slices align across the separator


def test_write_then_read_sidecar_roundtrip(tmp_path, monkeypatch):
    # Redirect the extracted-text dir into tmp_path.
    import ycl._paths as paths

    monkeypatch.setattr(paths, "EXTRACTED_DIR", tmp_path / "extracted")

    chapters = _chapters()
    result = ScrapeResult(
        book_id="onc5689", isbn="9780310522744", title="The Real Title", chapters=chapters
    )
    write_text_cache("lib", "onc5689", result)

    text = paths.text_path_for("lib", "onc5689").read_text(encoding="utf-8")
    assert text == result.text

    title, rebuilt = read_chapter_sidecar("lib", "onc5689", text)
    assert title == "The Real Title"
    assert [c.title for c in rebuilt] == [None, "Chapter One", "Chapter Two"]
    assert [c.text for c in rebuilt] == [c.text for c in chapters]


def test_read_sidecar_absent_returns_empty(tmp_path, monkeypatch):
    import ycl._paths as paths

    monkeypatch.setattr(paths, "EXTRACTED_DIR", tmp_path / "extracted")
    title, chapters = read_chapter_sidecar("lib", "missing", "some text")
    assert title is None
    assert chapters == []
