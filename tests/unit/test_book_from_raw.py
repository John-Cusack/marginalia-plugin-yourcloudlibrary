"""Unit tests for the shared detail-loader Book mapping."""

from __future__ import annotations

from ycl.api.client import _book_from_raw

_RAW = {
    "itemId": "onc5689",
    "isbn": "9780310522744",
    "title": "Four Views on the Church's Mission",
    "status": "LOAN",
    "canRead": "true",
    "page": "320",
    "publisher": "Zondervan",
    "language": "en",
    "mediaType": "ebook",
}


def test_book_from_raw_maps_all_fields():
    b = _book_from_raw(_RAW, "fallback")
    assert b.item_id == "onc5689"
    assert b.isbn == "9780310522744"
    assert b.title == "Four Views on the Church's Mission"
    assert b.status == "LOAN"
    assert b.can_read is True
    assert b.page_count == 320
    assert b.publisher == "Zondervan"
    assert b.raw is _RAW


def test_book_from_raw_falls_back_and_coerces():
    b = _book_from_raw({}, "the-book-id")
    assert b.item_id == "the-book-id"   # itemId missing → fallback
    assert b.isbn == ""
    assert b.title == "Untitled"
    assert b.status == ""
    assert b.can_read is False          # canRead absent → False
    assert b.page_count is None


def test_can_read_accepts_bool_and_string_true():
    assert _book_from_raw({"canRead": True}, "x").can_read is True
    assert _book_from_raw({"canRead": "True"}, "x").can_read is True
    assert _book_from_raw({"canRead": "false"}, "x").can_read is False
    assert _book_from_raw({"canRead": False}, "x").can_read is False
