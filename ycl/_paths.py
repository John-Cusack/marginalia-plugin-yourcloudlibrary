"""Where the plugin keeps mutable state: session cookies, borrow registry, extracts.

Core owns the location. Inside the host every tool and the source-search provider
receive ``PluginContext.data_dir``; :func:`resolve_paths` turns that into the
concrete files. Code that runs outside the host — the ``research-engine-ycl-login``
console script — has no context, so :func:`default_data_dir` reproduces core's
default and honours the same overrides. Nothing here derives a path from the
installed package location.

Layout under the data directory::

    cookies.json                          # YCL session (0600)
    borrows.json, borrows.json.lock       # loan registry
    extracted/{library}/{book_id}.txt     # canonical scraped text (licensed content)
    extracted/{library}/{book_id}.chapters.json
    extracted/{library}/{book_id}.partial.txt
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_ID = "yourcloudlibrary"

# Pre-0.3.0 location. Read only by the explicit migration command and by the
# ingest idempotency check (documents ingested before migration recorded a
# source under this directory).
LEGACY_DATA_DIR = Path.home() / ".marginalia" / "plugins" / PLUGIN_ID

# Core's ``data_dir`` setting. Core passes ``<data_dir>/plugin-data/<plugin_id>``.
CORE_DATA_DIR_ENV = "RE_DATA_DIR"


def default_data_dir() -> Path:
    """The directory core passes as ``PluginContext.data_dir`` for this plugin.

    Mirrors core's default and its ``RE_DATA_DIR`` environment override. A data
    directory configured only in core's ``.env`` file is invisible here; pass
    ``--data-dir`` to the console command in that case.
    """
    core = os.environ.get(CORE_DATA_DIR_ENV)
    base = Path(core).expanduser() if core else Path.home() / ".research-engine"
    return base / "plugin-data" / PLUGIN_ID


@dataclass(frozen=True)
class PluginPaths:
    """Every on-disk location, derived from one data directory."""

    data_dir: Path

    @property
    def cookie_path(self) -> Path:
        return self.data_dir / "cookies.json"

    @property
    def borrows_path(self) -> Path:
        return self.data_dir / "borrows.json"

    @property
    def extracted_dir(self) -> Path:
        return self.data_dir / "extracted"

    def text_path_for(self, library_id: str, book_id: str) -> Path:
        """Canonical on-disk path for a scraped book's text."""
        return self.extracted_dir / library_id / f"{book_id}.txt"

    def chapters_path_for(self, library_id: str, book_id: str) -> Path:
        """Sidecar holding chapter structure for the cached text (see _textcache)."""
        return self.extracted_dir / library_id / f"{book_id}.chapters.json"

    def partial_path_for(self, library_id: str, book_id: str) -> Path:
        """Checkpoint path for an in-progress scrape; one page per write."""
        return self.extracted_dir / library_id / f"{book_id}.partial.txt"


def resolve_paths(context: Any = None) -> PluginPaths:
    """Paths for ``context.data_dir``, or core's default when there is no context."""
    data_dir = getattr(context, "data_dir", None)
    return PluginPaths(Path(data_dir) if data_dir is not None else default_data_dir())


def legacy_paths() -> PluginPaths:
    return PluginPaths(LEGACY_DATA_DIR)
