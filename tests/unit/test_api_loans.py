"""Tests for the active-loans flow and detail-page author/subject parsing.

Uses ``httpx.MockTransport`` so nothing touches the network. The live shape
these mocks mirror was confirmed on 2026-06-29 (see scripts/probe_loans.py):
``POST /library/{slug}/mybooks/current?...&_data=routes/library.$name.mybooks.current``
returns ``{"patronItems": [...], "totalSegments": N, ...}``.
"""

from __future__ import annotations

import httpx
import pytest

from ycl.api.client import (
    YclClient,
    _extract_author,
    _extract_subjects,
)
from ycl.api.errors import AuthExpiredError
from ycl.api.types import LibraryInfo

LIBRARY = LibraryInfo(
    name="Palm Beach County Library System",
    url_name="PalmBeachCountyLibrarySystem",
    library_uuid="793edfa10e6743fc8ce5cf6b1b4147bf",
)
JAR = {"__session_PROD": "fake", "__config_PROD": "fake"}


def _client_with_handler(handler) -> YclClient:
    client = YclClient(cookie_jar=JAR, library_info=LIBRARY)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=JAR,
        follow_redirects=True,
        timeout=10.0,
    )
    return client


def _loan_item(item_id: str, *, due="2026-07-10T16:22:54Z", title="A Book") -> dict:
    return {
        "itemId": item_id,
        "loanId": f"loan-{item_id}",
        "title": title,
        "mediaType": "Epub",
        "dueDate": due,
        "author": "Some, Author",
        "canRenew": True,
        "canReturn": True,
        "isSaved": False,
    }


# ----- get_loans -----------------------------------------------------------


async def test_get_loans_posts_action_and_parses_items():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["data_param"] = request.url.params.get("_data")
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "patronItems": [_loan_item("onc1"), _loan_item("onc2")],
                "totalSegments": 1,
                "currentSegment": 1,
                "itemsPerSegment": 20,
                "totalItems": 2,
            },
        )

    client = _client_with_handler(handler)
    async with client:
        loans = await client.get_loans()

    assert seen["method"] == "POST"
    assert seen["path"] == "/library/PalmBeachCountyLibrarySystem/mybooks/current"
    assert seen["data_param"] == "routes/library.$name.mybooks.current"
    assert "sort=BorrowedDateDescending" in seen["body"]
    assert [loan.item_id for loan in loans] == ["onc1", "onc2"]
    first = loans[0]
    assert first.loan_id == "loan-onc1"
    assert first.due_date == "2026-07-10T16:22:54Z"
    assert first.author == "Some, Author"
    assert first.media_type == "Epub"
    assert first.can_renew is True


async def test_get_loans_walks_all_segments():
    def handler(request: httpx.Request) -> httpx.Response:
        segment = int(request.url.params.get("segment", "1"))
        return httpx.Response(
            200,
            json={
                "patronItems": [_loan_item(f"seg{segment}")],
                "totalSegments": 3,
                "currentSegment": segment,
            },
        )

    client = _client_with_handler(handler)
    async with client:
        loans = await client.get_loans()

    # One item per segment, three segments → three loans, in segment order.
    assert [loan.item_id for loan in loans] == ["seg1", "seg2", "seg3"]


async def test_get_loans_dedups_when_server_ignores_segment():
    # Server claims 3 segments but ignores the param and re-serves the same page.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "patronItems": [_loan_item("onc1"), _loan_item("onc2")],
                "totalSegments": 3,
            },
        )

    client = _client_with_handler(handler)
    async with client:
        loans = await client.get_loans()
    # Deduped by item_id despite the multi-segment claim.
    assert [loan.item_id for loan in loans] == ["onc1", "onc2"]


async def test_get_loans_stops_on_empty_segment():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        segment = int(request.url.params.get("segment", "1"))
        items = [_loan_item("onc1")] if segment == 1 else []
        return httpx.Response(200, json={"patronItems": items, "totalSegments": 9})

    client = _client_with_handler(handler)
    async with client:
        loans = await client.get_loans()
    assert [loan.item_id for loan in loans] == ["onc1"]
    # Stopped after the first empty page rather than paging to segment 9.
    assert calls["n"] == 2


async def test_get_loans_empty_when_no_active_loans():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"patronItems": [], "totalSegments": 1})

    client = _client_with_handler(handler)
    async with client:
        loans = await client.get_loans()
    assert loans == []


async def test_get_loans_auth_expired_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    client = _client_with_handler(handler)
    with pytest.raises(AuthExpiredError):
        async with client:
            await client.get_loans()


# ----- get_book author / subjects / description ----------------------------


async def test_get_book_extracts_author_subjects_description():
    payload = {
        "book": {
            "itemId": "onc5689",
            "isbn": "9780310522744",
            "title": "Four Views on the Church's Mission",
            "status": "LOAN",
            "canRead": True,
            "page": 208,
            "publisher": "Zondervan",
            "language": "en",
            "mediaType": "Epub",
            "contributors": [
                {"name": "Leeman, Jonathan; Wright, Christopher J. H.; Zondervan,"},
                {},
            ],
            "contentCategories": {
                "aw6hw": {"name": "Ecclesiology"},
                "t69w": {"name": "Missions"},
                "dup": {"name": "Ecclesiology"},
                "blank": {"name": ""},
            },
            "description": "<p>A debate on the church's mission.</p>",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_handler(handler)
    async with client:
        book = await client.get_book("onc5689")

    # Trailing comma stripped, both contributor names joined.
    assert book.author == "Leeman, Jonathan; Wright, Christopher J. H.; Zondervan"
    # De-duplicated, blanks dropped, order preserved.
    assert book.subjects == ["Ecclesiology", "Missions"]
    assert book.description == "<p>A debate on the church's mission.</p>"


# ----- pure helpers --------------------------------------------------------


def test_extract_author_prefers_contributors_over_flat_author():
    assert _extract_author({"contributors": [{"name": "Doe, Jane"}], "author": "x"}) == (
        "Doe, Jane"
    )


def test_extract_author_falls_back_to_flat_author():
    assert _extract_author({"author": "Doe, Jane"}) == "Doe, Jane"


def test_extract_author_returns_none_when_absent():
    assert _extract_author({}) is None
    assert _extract_author({"contributors": [{}], "author": ""}) is None


def test_extract_subjects_handles_non_dict():
    assert _extract_subjects(None) == []
    assert _extract_subjects([1, 2, 3]) == []


def test_extract_subjects_dedups_whitespace_variants():
    cats = {
        "a": {"name": "Missions"},
        "b": {"name": " Missions "},
        "c": {"name": "Ecclesiology"},
    }
    assert _extract_subjects(cats) == ["Missions", "Ecclesiology"]
