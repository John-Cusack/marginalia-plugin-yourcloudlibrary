"""Shared filesystem paths for the YourCloudLibrary plugin."""

from __future__ import annotations

from pathlib import Path

PLUGIN_DATA_DIR = Path.home() / ".marginalia" / "plugins" / "yourcloudlibrary"
COOKIE_PATH = PLUGIN_DATA_DIR / "cookies.json"
BORROWS_PATH = PLUGIN_DATA_DIR / "borrows.json"
EXTRACTED_DIR = PLUGIN_DATA_DIR / "extracted"


def text_path_for(library_id: str, book_id: str) -> Path:
    """Canonical on-disk path for a scraped book's text."""
    return EXTRACTED_DIR / library_id / f"{book_id}.txt"


def chapters_path_for(library_id: str, book_id: str) -> Path:
    """Sidecar holding chapter structure for the cached text (see _textcache)."""
    return EXTRACTED_DIR / library_id / f"{book_id}.chapters.json"


def partial_path_for(library_id: str, book_id: str) -> Path:
    """Checkpoint path for an in-progress scrape; one page per write."""
    return EXTRACTED_DIR / library_id / f"{book_id}.partial.txt"
