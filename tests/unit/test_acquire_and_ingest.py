"""yourcloudlibrary.acquire_and_ingest — loan safety and the shared ingestion boundary.

No network, no core, no live book is borrowed: the YCL client is a fake whose
loan state is a dict, and ingestion goes through a fake SDK IngestionClient.

Invariants:
- a loan this call opened is ALWAYS returned — on success, on ingest error, and
  on cancellation (which still propagates);
- a loan the user already had is never returned;
- a failed return never undoes an ingest; it is reported;
- nothing is borrowed when the book is already ingested or reading is dead.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ycl.tools._ingest as boundary
import ycl.tools.acquire_and_ingest as mod
from ycl.api.errors import YclApiError
from ycl.session.cookies import CookieStore

LIB = "LIB"


class _Loans:
    """Server-side loan state shared by every fake client instance."""

    def __init__(self, *, on_loan: set[str] | None = None, return_fails: bool = False):
        self.on_loan = set(on_loan or ())
        self.return_fails = return_fails
        self.borrowed: list[str] = []
        self.returned: list[str] = []
        self.cookie_paths: list = []


def _client_class(loans: _Loans):
    class _FakeClient:
        library = SimpleNamespace(url_name=LIB, name="Lib")

        @classmethod
        def from_cookie_store(cls, path):
            loans.cookie_paths.append(path)
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def close(self):
            pass

        def _book(self, book_id):
            status = "LOAN" if book_id in loans.on_loan else "CAN_LOAN"
            return SimpleNamespace(status=status, can_read=status == "LOAN",
                                   raw={"canBorrow": True})

        async def get_book(self, book_id):
            return self._book(book_id)

        async def borrow(self, book_id):
            loans.borrowed.append(book_id)
            loans.on_loan.add(book_id)
            return self._book(book_id)

        async def return_book(self, book_id):
            if loans.return_fails:
                raise YclApiError("return refused: server error")
            loans.returned.append(book_id)
            loans.on_loan.discard(book_id)
            return self._book(book_id)

    return _FakeClient


class _FakeIngestion:
    def __init__(self, existing=None):
        self.existing = existing or []

    async def find_existing(self, *, source=None, source_pattern=None):
        return self.existing


@pytest.fixture
def loans(monkeypatch, plugin_paths):
    state = _Loans()
    monkeypatch.setattr(mod, "YclClient", _client_class(state))
    # A live, unexpired session on disk in the context's data directory.
    CookieStore(plugin_paths.cookie_path).save(
        [{"name": "__session_PROD", "value": "x", "expires": -1}]
    )
    return state


def _ingest_returning(result: dict, calls: list):
    async def fake(**kwargs):
        calls.append(kwargs)
        return result

    return fake


async def test_borrows_ingests_through_shared_boundary_and_returns(
    loans, monkeypatch, plugin_context, plugin_paths
):
    calls: list = []
    monkeypatch.setattr(
        boundary, "ingest_book",
        _ingest_returning({"status": "ingested", "document_id": "d1", "passage_count": 5}, calls),
    )

    res = await mod.handler(book_id="bk1", ingestion=_FakeIngestion(), context=plugin_context)

    assert res["status"] == "ingested"
    assert res["borrowed_by_us"] is True
    assert res["returned"] is True
    assert loans.borrowed == ["bk1"] and loans.returned == ["bk1"]
    assert calls[0]["paths"] == plugin_paths
    assert calls[0]["book_id"] == "bk1"
    assert set(loans.cookie_paths) == {plugin_paths.cookie_path}


async def test_loan_returned_even_when_ingest_cancelled(loans, monkeypatch, plugin_context):
    async def cancelled(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(boundary, "ingest_book", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await mod.handler(book_id="bk1", force_reingest=True, ingestion=_FakeIngestion(),
                          context=plugin_context)

    assert loans.returned == ["bk1"]


async def test_loan_returned_when_ingest_raises(loans, monkeypatch, plugin_context):
    async def boom(**kwargs):
        raise RuntimeError("duplicate content")

    monkeypatch.setattr(boundary, "ingest_book", boom)

    res = await mod.handler(book_id="bk2", force_reingest=True, ingestion=_FakeIngestion(),
                            context=plugin_context)

    assert res["error_type"] == "ingest_failed"
    assert res["borrowed_by_us"] is True
    assert res["returned"] is True
    assert res.get("return_failed") is not True


async def test_failed_return_keeps_ingest_and_is_reported(loans, monkeypatch, plugin_context):
    loans.return_fails = True
    monkeypatch.setattr(
        boundary, "ingest_book",
        _ingest_returning({"status": "ingested", "document_id": "d9", "passage_count": 5}, []),
    )

    res = await mod.handler(book_id="bk3", ingestion=_FakeIngestion(), context=plugin_context)

    # The corpus write stands; the leaked loan is surfaced, not hidden.
    assert res["status"] == "ingested"
    assert res["document_id"] == "d9"
    assert res["returned"] is False
    assert res["return_failed"] is True
    assert "bk3" in res["warning"]
    assert "bk3" in loans.on_loan


async def test_pre_existing_loan_is_never_returned(loans, monkeypatch, plugin_context):
    loans.on_loan.add("mine")
    monkeypatch.setattr(
        boundary, "ingest_book",
        _ingest_returning({"status": "ingested", "document_id": "d2", "passage_count": 1}, []),
    )

    res = await mod.handler(book_id="mine", ingestion=_FakeIngestion(), context=plugin_context)

    assert res["borrowed_by_us"] is False
    assert res["returned"] is False
    assert loans.borrowed == [] and loans.returned == []


async def test_already_ingested_skips_borrow(loans, monkeypatch, plugin_context):
    async def must_not_ingest(**kwargs):
        raise AssertionError("should not ingest")

    monkeypatch.setattr(boundary, "ingest_book", must_not_ingest)
    ingestion = _FakeIngestion(existing=[{"document_id": "old", "title": "T"}])

    res = await mod.handler(book_id="bk4", ingestion=ingestion, context=plugin_context)

    assert res["status"] == "already_ingested"
    assert res["document_id"] == "old"
    assert loans.borrowed == []


async def test_expired_reading_session_fails_before_borrowing(
    loans, monkeypatch, plugin_context, plugin_paths
):
    CookieStore(plugin_paths.cookie_path).save(
        [{"name": "__session_PROD", "value": "x", "expires": 1.0}]
    )

    res = await mod.handler(book_id="bk5", ingestion=_FakeIngestion(), context=plugin_context)

    assert res["error_type"] == "session_expired"
    assert loans.borrowed == []
