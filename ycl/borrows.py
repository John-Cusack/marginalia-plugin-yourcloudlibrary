"""Borrow lifecycle store — JSON-backed registry of every loan we know about.

State file layout (partitioned by ``library_id`` so switching libraries doesn't
collide IDs):

    {
      "<library_id>": {
        "<book_id>": {
          "book_id": ..., "library_id": ..., "title": ...,
          "borrowed_at": "<UTC ISO 8601 Z>",
          "expires_at": "<UTC ISO 8601 Z>",
          "expires_at_is_estimated": bool,
          "scraped": bool, "scraped_at": ..., "char_count": int,
          "ingested": bool, "document_id": "..."
        }
      }
    }

All public mutators take an exclusive ``fcntl.flock`` on the state file so two
concurrent tool invocations cannot lose updates.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ._paths import BORROWS_PATH
from ._time import from_iso, utcnow

_StateT = dict[str, dict[str, dict[str, Any]]]


class BorrowStore:
    """File-locked JSON store of borrow records.

    Each public method opens, locks, mutates, and atomically rewrites the file.
    The store does not cache state across calls — correctness over speed at
    this scale (a user has, at most, hundreds of borrows over many years).
    """

    def __init__(self, path: Path = BORROWS_PATH) -> None:
        self.path = Path(path)

    # --- read paths --------------------------------------------------------

    def get(self, library_id: str, book_id: str) -> dict[str, Any] | None:
        with self._shared_lock() as state:
            return state.get(library_id, {}).get(book_id)

    def list(self, library_id: str) -> list[dict[str, Any]]:
        with self._shared_lock() as state:
            return list(state.get(library_id, {}).values())

    def is_active(
        self,
        library_id: str,
        book_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        record = self.get(library_id, book_id)
        if record is None:
            return False
        expires_at = record.get("expires_at")
        if not expires_at:
            return False
        return from_iso(expires_at) > (now or utcnow())

    def days_remaining(
        self,
        library_id: str,
        book_id: str,
        *,
        now: datetime | None = None,
    ) -> int | None:
        record = self.get(library_id, book_id)
        if record is None or not record.get("expires_at"):
            return None
        delta = from_iso(record["expires_at"]) - (now or utcnow())
        # Round toward zero so a loan with 1.4 days left reports "1 day" rather
        # than "2"; expired loans report negative numbers.
        return int(delta.total_seconds() // 86400)

    # --- write paths -------------------------------------------------------

    def upsert(self, *, library_id: str, book_id: str, **fields: Any) -> dict[str, Any]:
        """Create or merge a borrow record. Returns the post-merge record."""
        with self._exclusive_lock() as state:
            shelf = state.setdefault(library_id, {})
            record = shelf.get(book_id, {})
            record.update(
                {"library_id": library_id, "book_id": book_id, **fields}
            )
            shelf[book_id] = record
            self._write(state)
            return dict(record)

    def mark_scraped(
        self,
        library_id: str,
        book_id: str,
        *,
        scraped_at: str,
        char_count: int,
    ) -> None:
        with self._exclusive_lock() as state:
            shelf = state.setdefault(library_id, {})
            record = shelf.setdefault(
                book_id, {"library_id": library_id, "book_id": book_id}
            )
            record["scraped"] = True
            record["scraped_at"] = scraped_at
            record["char_count"] = char_count
            self._write(state)

    def mark_ingested(
        self,
        library_id: str,
        book_id: str,
        *,
        document_id: str,
    ) -> None:
        with self._exclusive_lock() as state:
            shelf = state.setdefault(library_id, {})
            record = shelf.setdefault(
                book_id, {"library_id": library_id, "book_id": book_id}
            )
            record["ingested"] = True
            record["document_id"] = document_id
            self._write(state)

    def forget(self, library_id: str, book_id: str) -> bool:
        """Remove a borrow record. Returns True if a record was removed."""
        with self._exclusive_lock() as state:
            shelf = state.get(library_id, {})
            if book_id not in shelf:
                return False
            del shelf[book_id]
            if not shelf:
                state.pop(library_id, None)
            self._write(state)
            return True

    # --- locking + IO ------------------------------------------------------

    class _LockCtx:
        # The lock is held on a sidecar ``.lock`` file rather than the data
        # file itself. ``_write`` uses ``os.replace`` to swap inodes
        # atomically, which would break a flock held on the data file (the
        # next writer would lock a freshly-created inode and not see the
        # prior holder). The sidecar is never replaced, so flock on it
        # serializes writers across rewrites.
        def __init__(self, store: BorrowStore, exclusive: bool) -> None:
            self.store = store
            self.exclusive = exclusive
            self.fh: Any = None
            self.state: _StateT = {}

        def __enter__(self) -> _StateT:
            self.store.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.store.path.with_suffix(self.store.path.suffix + ".lock")
            self.fh = open(lock_path, "a+", encoding="utf-8")
            fcntl.flock(
                self.fh,
                fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH,
            )
            self.state = self.store._read_unlocked()
            return self.state

        def __exit__(self, *_exc: Any) -> None:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()

    def _exclusive_lock(self) -> BorrowStore._LockCtx:
        return BorrowStore._LockCtx(self, exclusive=True)

    def _shared_lock(self) -> BorrowStore._LockCtx:
        return BorrowStore._LockCtx(self, exclusive=False)

    def _read_unlocked(self) -> _StateT:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _write(self, state: _StateT) -> None:
        """Atomic write via tempfile in the same directory + os.replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
            prefix=".borrows-",
            suffix=".json.tmp",
        ) as tmp:
            json.dump(state, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, self.path)
