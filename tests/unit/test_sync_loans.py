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
    assert loan_out["expires_at"] is None
    assert loan_out["expires_at_is_estimated"] is True
    # Title still recorded even without a usable expiry.
    assert store.get(LIBRARY_KEY, "onc1")["title"] == "Mystery"


async def test_sync_loans_skips_items_without_id(store, monkeypatch):
    loans = [
        Loan(item_id="", title="ghost", due_date="2099-01-01T00:00:00Z"),
        Loan(item_id="onc2", title="real", due_date="2099-01-01T00:00:00Z"),
    ]
    _install_client(monkeypatch, _FakeClient(loans))

    result = await handler()

    assert result["active_loan_count"] == 1
    assert result["loans"][0]["book_id"] == "onc2"


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
