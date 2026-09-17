"""The shared ingestion boundary against real core ingestion and a disposable DB."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ycl.tools.acquire_and_ingest as acquire
from tests.integration.conftest import FIXTURE_LIBRARY
from ycl.api.errors import YclApiError
from ycl.tools import ingest_book

pytestmark = pytest.mark.integration


async def test_fixture_book_stores_canonical_text_and_passages(seeded_context, ingestion):
    context, book = seeded_context

    result = await ingest_book.handler(book_id=book.book_id, ingestion=ingestion, context=context)

    assert result["status"] == "ingested", result
    assert result["passage_count"] > 0
    assert await ingestion.stored_text(result["document_id"]) == book.text
    (doc,) = await ingestion.find_existing(source=result["source"])
    assert doc["document_id"] == result["document_id"]
    assert doc["document_type"] == "ycl_book"
    assert doc["metadata"]["book_id"] == book.book_id
    assert doc["metadata"]["library_id"] == FIXTURE_LIBRARY
    assert doc["metadata"]["char_count"] == len(book.text)


async def test_repeat_returns_the_existing_document(seeded_context, ingestion):
    context, book = seeded_context

    first = await ingest_book.handler(book_id=book.book_id, ingestion=ingestion, context=context)
    second = await ingest_book.handler(book_id=book.book_id, ingestion=ingestion, context=context)

    assert second["status"] == "already_ingested"
    assert second["document_id"] == first["document_id"]
    assert ingestion.created == [first["document_id"]]


async def test_failed_loan_return_keeps_the_ingested_document(
    seeded_context, ingestion, monkeypatch
):
    """Borrow → ingest (real) → return fails: the document stays and it is reported."""
    context, book = seeded_context
    on_loan: set[str] = set()

    class _Client:
        library = SimpleNamespace(url_name=FIXTURE_LIBRARY, name="Fixture Library")

        @classmethod
        def from_cookie_store(cls, path):
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def close(self):
            pass

        async def get_book(self, book_id):
            status = "LOAN" if book_id in on_loan else "CAN_LOAN"
            return SimpleNamespace(status=status, can_read=False, raw={"canBorrow": True})

        async def borrow(self, book_id):
            on_loan.add(book_id)
            return SimpleNamespace(status="LOAN", can_read=True, raw={})

        async def return_book(self, book_id):
            raise YclApiError("return refused")

    monkeypatch.setattr(acquire, "YclClient", _Client)

    result = await acquire.handler(book_id=book.book_id, ingestion=ingestion, context=context)

    assert result["status"] == "ingested", result
    assert result["return_failed"] is True
    assert book.book_id in result["warning"]
    assert await ingestion.find_existing(source_pattern=book.book_id)
