"""BorrowStore: roundtrip, library partitioning, expiration arithmetic, concurrency."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from ycl.borrows import BorrowStore


@pytest.fixture
def store(tmp_path):
    return BorrowStore(path=tmp_path / "borrows.json")


def test_get_returns_none_for_missing(store):
    assert store.get("lapl", "onc5689") is None


def test_upsert_creates_record(store):
    record = store.upsert(
        library_id="lapl",
        book_id="onc5689",
        title="The Power Broker",
        expires_at="2026-05-21T12:00:00Z",
    )
    assert record["library_id"] == "lapl"
    assert record["book_id"] == "onc5689"
    assert record["title"] == "The Power Broker"
    assert record["expires_at"] == "2026-05-21T12:00:00Z"


def test_upsert_merges_fields(store):
    store.upsert(library_id="lapl", book_id="onc5689", title="Initial")
    store.upsert(library_id="lapl", book_id="onc5689", expires_at="2026-05-21T12:00:00Z")
    record = store.get("lapl", "onc5689")
    assert record["title"] == "Initial"
    assert record["expires_at"] == "2026-05-21T12:00:00Z"


def test_list_returns_only_requested_library(store):
    store.upsert(library_id="lapl", book_id="onc5689", title="A")
    store.upsert(library_id="nypl", book_id="abc1234", title="B")
    lapl = store.list("lapl")
    nypl = store.list("nypl")
    assert {b["book_id"] for b in lapl} == {"onc5689"}
    assert {b["book_id"] for b in nypl} == {"abc1234"}


def test_is_active_respects_expires_at(store):
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    store.upsert(
        library_id="lapl",
        book_id="future",
        expires_at="2026-05-21T12:00:00Z",
    )
    store.upsert(
        library_id="lapl",
        book_id="past",
        expires_at="2026-04-30T12:00:00Z",
    )
    assert store.is_active("lapl", "future", now=now) is True
    assert store.is_active("lapl", "past", now=now) is False
    # Missing book → not active (does not raise).
    assert store.is_active("lapl", "missing", now=now) is False


def test_days_remaining(store):
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    store.upsert(
        library_id="lapl",
        book_id="onc5689",
        expires_at="2026-05-21T12:00:00Z",  # 14 days from now
    )
    assert store.days_remaining("lapl", "onc5689", now=now) == 14
    # Expired loan → negative
    store.upsert(
        library_id="lapl",
        book_id="past",
        expires_at="2026-04-30T12:00:00Z",  # 7 days ago, on the dot
    )
    assert store.days_remaining("lapl", "past", now=now) == -7


def test_days_remaining_returns_none_when_unknown(store):
    assert store.days_remaining("lapl", "missing") is None


def test_mark_scraped_and_ingested(store):
    store.upsert(library_id="lapl", book_id="onc5689", title="A")
    store.mark_scraped(
        "lapl", "onc5689", scraped_at="2026-05-07T12:00:00Z", char_count=1234
    )
    store.mark_ingested("lapl", "onc5689", document_id="doc-uuid-1")
    record = store.get("lapl", "onc5689")
    assert record["scraped"] is True
    assert record["scraped_at"] == "2026-05-07T12:00:00Z"
    assert record["char_count"] == 1234
    assert record["ingested"] is True
    assert record["document_id"] == "doc-uuid-1"


def test_forget_removes_record(store):
    store.upsert(library_id="lapl", book_id="onc5689", title="A")
    assert store.forget("lapl", "onc5689") is True
    assert store.get("lapl", "onc5689") is None
    # Forgetting an unknown record is a noop.
    assert store.forget("lapl", "onc5689") is False


def test_forget_cleans_up_empty_library_shelf(store):
    store.upsert(library_id="lapl", book_id="onc5689", title="A")
    store.forget("lapl", "onc5689")
    # The "lapl" shelf should be gone, not just empty — keeps the JSON tidy.
    import json

    state = json.loads(store.path.read_text())
    assert "lapl" not in state


def test_atomic_write_preserves_data_across_processes(store, tmp_path):
    """Verify writes survive a re-instantiation of the store."""
    store.upsert(library_id="lapl", book_id="onc5689", title="A")
    store2 = BorrowStore(path=store.path)
    assert store2.get("lapl", "onc5689")["title"] == "A"


def test_concurrent_upserts_do_not_lose_updates(store):
    """Many threads writing different books should all land in the store."""
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def worker(i: int):
        try:
            barrier.wait()
            store.upsert(
                library_id="lapl",
                book_id=f"book{i:03d}",
                title=f"Title {i}",
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    landed = {b["book_id"] for b in store.list("lapl")}
    assert landed == {f"book{i:03d}" for i in range(n_threads)}
