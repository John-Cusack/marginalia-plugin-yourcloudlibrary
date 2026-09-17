"""Loan-safety unit tests for acquire_and_ingest (no network/DB).

Verifies the invariant: a loan we open is ALWAYS returned — even when the ingest
step is cancelled (host timeout/shutdown) — and the CancelledError still
propagates.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ycl.tools.acquire_and_ingest as mod


class _FakeClient:
    """Stand-in YclClient; records whether the loan was returned."""

    returns: list[str] = []

    def __init__(self) -> None:
        self.library = SimpleNamespace(url_name="LIB", name="Lib")

    @classmethod
    def from_cookie_store(cls) -> _FakeClient:
        return cls()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def close(self):
        pass

    async def get_book(self, book_id):
        # Not on loan, borrowable, then (after a return) reverted.
        status = "CAN_LOAN" if book_id in _FakeClient.returns else "CAN_LOAN"
        return SimpleNamespace(status=status, can_read=False, raw={"canBorrow": True})

    async def borrow(self, book_id):
        return SimpleNamespace(status="LOAN", can_read=True, raw={})

    async def return_book(self, book_id):
        _FakeClient.returns.append(book_id)
        return SimpleNamespace(status="CAN_LOAN", can_read=False, raw={})


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _FakeClient.returns = []
    monkeypatch.setattr(mod, "YclClient", _FakeClient)
    monkeypatch.setattr(mod, "BorrowStore", lambda: SimpleNamespace(upsert=lambda **k: None))
    monkeypatch.setattr(
        mod, "CookieStore", lambda *a, **k: SimpleNamespace(load=lambda: [{"name": "__session_PROD"}])
    )
    monkeypatch.setattr(mod, "reading_session_status", lambda *a, **k: {"ok": True})


async def test_loan_returned_even_when_ingest_cancelled(monkeypatch):
    async def _cancelled(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "ingest_book_handler", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await mod.handler(book_id="bk1", return_after=True, force_reingest=True, ingestion=object())

    # The loan we opened was returned despite the cancellation — no leak.
    assert "bk1" in _FakeClient.returns


async def test_loan_returned_when_ingest_raises_normal_error(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("duplicate content")

    monkeypatch.setattr(mod, "ingest_book_handler", _boom)

    res = await mod.handler(book_id="bk2", return_after=True, force_reingest=True, ingestion=object())
    assert res["borrowed_by_us"] is True
    assert res["returned"] is True
    assert "bk2" in _FakeClient.returns
    assert res.get("return_failed") is not True
