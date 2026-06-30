"""Tests for the ycl.sync_loans tool handler.

The handler is exercised with a fake YclClient (no cookies, no network) and a
BorrowStore pointed at a tmp file, so we can assert that real ``dueDate``
values land in the store with the estimated flag cleared.
"""

from __future__ import annotations

import pytest

import ycl.tools.sync_loans as mod
from ycl.api.errors import AuthExpiredError, NotAuthenticatedError
from ycl.api.types import Loan
from ycl.borrows import BorrowStore
from ycl.tools.sync_loans import _normalize_due_date, handler

LIBRARY_KEY = "PalmBeachCountyLibrarySystem"


class _FakeLibrary:
    url_name = LIBRARY_KEY
    name = "Palm Beach County Library System"


class _FakeClient:
    def __init__(self, loans=None, error=None):
        self._loans = loans or []
        self._error = error
        self.library = _FakeLibrary()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def close(self):
        self.closed = True

    async def get_loans(self):
        if self._error is not None:
            raise self._error
        return self._loans


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = BorrowStore(tmp_path / "borrows.json")
    monkeypatch.setattr(mod, "BorrowStore", lambda: s)
    return s


def _install_client(monkeypatch, client):
    monkeypatch.setattr(
        mod.YclClient, "from_cookie_store", staticmethod(lambda *a, **k: client)
    )


async def test_sync_loans_writes_real_expiry_and_clears_estimate(store, monkeypatch):
    loans = [
        Loan(
            item_id="onc5689",
            title="Four Views",
            due_date="2099-01-10T16:22:54Z",
            loan_id="loan-1",
            media_type="Epub",
            author="Doe, Jane",
        ),
    ]
    _install_client(monkeypatch, _FakeClient(loans))

    result = await handler()

    assert result["status"] == "synced"
    assert result["active_loan_count"] == 1
    loan_out = result["loans"][0]
    assert loan_out["book_id"] == "onc5689"
    assert loan_out["expires_at"] == "2099-01-10T16:22:54Z"
    assert loan_out["expires_at_is_estimated"] is False
    assert loan_out["author"] == "Doe, Jane"

    record = store.get(LIBRARY_KEY, "onc5689")
    assert record["expires_at"] == "2099-01-10T16:22:54Z"
    assert record["expires_at_is_estimated"] is False
    assert record["title"] == "Four Views"
    assert record["loan_id"] == "loan-1"


async def test_sync_loans_overwrites_a_prior_estimate(store, monkeypatch):
    # Seed an estimated record, as record_borrow/ingest would have left it.
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc5689",
        title="Four Views",
        expires_at="2026-05-21T00:00:00Z",
        expires_at_is_estimated=True,
    )
    loans = [Loan(item_id="onc5689", title="Four Views", due_date="2099-02-01T00:00:00Z")]
    _install_client(monkeypatch, _FakeClient(loans))

    await handler()

    record = store.get(LIBRARY_KEY, "onc5689")
    assert record["expires_at"] == "2099-02-01T00:00:00Z"
    assert record["expires_at_is_estimated"] is False


async def test_sync_loans_handles_unparseable_due_date(store, monkeypatch):
    loans = [Loan(item_id="onc1", title="Mystery", due_date="not-a-date")]
    _install_client(monkeypatch, _FakeClient(loans))

    result = await handler()

    loan_out = result["loans"][0]
    # An unparseable dueDate falls back to an estimate rather than a missing
    # expiry, so the book still reads as actively checked out.
    assert loan_out["expires_at"] is not None
    assert loan_out["expires_at_is_estimated"] is True
    assert loan_out["days_remaining"] is not None
    record = store.get(LIBRARY_KEY, "onc1")
    assert record["title"] == "Mystery"
    assert record["expires_at"] is not None
    assert record["expires_at_is_estimated"] is True
    # The record is active (not erroneously reported as expired/returned).
    assert store.is_active(LIBRARY_KEY, "onc1") is True


async def test_sync_loans_skips_items_without_id(store, monkeypatch):
    loans = [
        Loan(item_id="", title="ghost", due_date="2099-01-01T00:00:00Z"),
        Loan(item_id="onc2", title="real", due_date="2099-01-01T00:00:00Z"),
    ]
    _install_client(monkeypatch, _FakeClient(loans))

    result = await handler()

    assert result["active_loan_count"] == 1
    assert result["loans"][0]["book_id"] == "onc2"


async def test_sync_loans_does_not_clobber_real_title_with_untitled(store, monkeypatch):
    # A prior ingest recorded the correct title.
    store.upsert(library_id=LIBRARY_KEY, book_id="onc5689", title="Real Title")
    # The loans payload omitted a title, so _loan_from_item defaulted it.
    loans = [Loan(item_id="onc5689", title="Untitled", due_date="2099-01-01T00:00:00Z")]
    _install_client(monkeypatch, _FakeClient(loans))

    await handler()

    assert store.get(LIBRARY_KEY, "onc5689")["title"] == "Real Title"


async def test_sync_loans_reconciles_returned_loan(store, monkeypatch):
    # A previously-synced loan (has a loan_id) with a still-future expiry.
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc_gone",
        title="Returned Early",
        loan_id="loan-gone",
        expires_at="2099-01-01T00:00:00Z",
        expires_at_is_estimated=False,
    )
    # A manually-recorded book (no loan_id) that must NOT be touched.
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc_manual",
        title="Manual",
        expires_at="2099-01-01T00:00:00Z",
        expires_at_is_estimated=False,
    )
    # The live list no longer contains onc_gone.
    loans = [Loan(item_id="onc_live", title="Still Out", due_date="2099-02-01T00:00:00Z")]
    _install_client(monkeypatch, _FakeClient(loans))

    result = await handler()

    assert result["returned_count"] == 1
    assert result["returned_book_ids"] == ["onc_gone"]
    gone = store.get(LIBRARY_KEY, "onc_gone")
    assert gone["returned"] is True
    assert gone["returned_at"] is not None
    assert store.is_active(LIBRARY_KEY, "onc_gone") is False
    # Manually-recorded book untouched and still active.
    manual = store.get(LIBRARY_KEY, "onc_manual")
    assert "returned" not in manual
    assert store.is_active(LIBRARY_KEY, "onc_manual") is True


async def test_sync_loans_reborrow_clears_returned_flag(store, monkeypatch):
    store.upsert(
        library_id=LIBRARY_KEY,
        book_id="onc5689",
        title="Came Back",
        loan_id="loan-old",
        returned=True,
        returned_at="2026-06-01T00:00:00Z",
        expires_at="2026-06-01T00:00:00Z",
        expires_at_is_estimated=False,
    )
    loans = [Loan(item_id="onc5689", title="Came Back", due_date="2099-03-01T00:00:00Z")]
    _install_client(monkeypatch, _FakeClient(loans))

    await handler()

    record = store.get(LIBRARY_KEY, "onc5689")
    assert "returned" not in record
    assert "returned_at" not in record
    assert store.is_active(LIBRARY_KEY, "onc5689") is True


async def test_sync_loans_not_authenticated(monkeypatch):
    def _raise(*_a, **_k):
        raise NotAuthenticatedError("no cookies")

    monkeypatch.setattr(mod.YclClient, "from_cookie_store", staticmethod(_raise))

    result = await handler()
    assert result["status"] == "error"
    assert result["error_type"] == "not_authenticated"


async def test_sync_loans_auth_expired(store, monkeypatch):
    _install_client(monkeypatch, _FakeClient(error=AuthExpiredError("expired")))

    result = await handler()
    assert result["status"] == "error"
    assert result["error_type"] == "auth_expired"


# ----- _normalize_due_date -------------------------------------------------


def test_normalize_due_date_iso_with_z():
    assert _normalize_due_date("2026-07-10T16:22:54Z") == "2026-07-10T16:22:54Z"


def test_normalize_due_date_iso_with_offset_to_utc():
    assert _normalize_due_date("2026-07-10T12:22:54-04:00") == "2026-07-10T16:22:54Z"


def test_normalize_due_date_epoch_millis():
    # 1783095774 s == 1783095774000 ms == 2026-07-03T16:22:54Z
    assert _normalize_due_date("1783095774000") == "2026-07-03T16:22:54Z"


def test_normalize_due_date_empty_and_garbage():
    assert _normalize_due_date("") is None
    assert _normalize_due_date("not-a-date") is None
