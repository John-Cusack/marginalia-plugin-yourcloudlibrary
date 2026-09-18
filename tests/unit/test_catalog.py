"""Unit tests for catalog search result mapping (no network/browser)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from ycl.api.catalog import CatalogItem, _build_search_url, _sanitize_cookies

# A representative item from results.search.items (trimmed).
_ITEM = {
    "id": "ispil6oqn",
    "bibliographicIdentifier": "ispil6oqn",
    "documentId": "onc5689",
    "catalogItemId": "dig-bibg-clispil6oqn",
    "isbn": "9780310522744",
    "title": "Four Views on the Church's Mission",
    "subtitle": "Counterpoints",
    "authors": ["Bird, Michael F."],
    "contributors": [{"name": "Bird, Michael F.", "role": 0}],
    "yearPublished": 2017,
    "format": "digital",
    "summary": "A book.",
    "language": "en",
    "publisherName": "Zondervan",
    "totalCopies": 1,
    "currentlyAvailable": 1,
    "currentlyLoaned": 0,
    "isPayPerUse": False,
    "matchingScore": 42.5,
    "imageLinkThumbnail": "http://img/x.jpg",
}


def test_from_search_item_uses_document_id_as_book_id():
    it = CatalogItem.from_search_item(_ITEM)
    # documentId is the borrow/ingest id, NOT id/bibliographicIdentifier.
    assert it.document_id == "onc5689"
    assert it.isbn == "9780310522744"
    assert it.title == "Four Views on the Church's Mission"
    assert it.authors == ["Bird, Michael F."]
    assert it.year == 2017
    assert it.matching_score == 42.5


def test_authors_fall_back_to_contributors():
    item = dict(_ITEM)
    item.pop("authors")
    it = CatalogItem.from_search_item(item)
    assert it.authors == ["Bird, Michael F."]


def test_is_available_now():
    it = CatalogItem.from_search_item(_ITEM)
    assert it.is_available_now is True

    out = CatalogItem.from_search_item({**_ITEM, "currentlyAvailable": 0})
    assert out.is_available_now is False

    ppu = CatalogItem.from_search_item({**_ITEM, "isPayPerUse": True})
    assert ppu.is_available_now is False  # pay-per-use is never "available to borrow"


def test_search_url_percent_encodes_reserved_chars():
    # A title with '&' must not split into bogus query params.
    url = _build_search_url("PalmBeachCountyLibrarySystem", "Sense & Sensibility")
    qs = parse_qs(urlsplit(url).query)
    assert qs["query"] == ["Sense & Sensibility"]  # round-trips intact
    assert "%20" in url or "+" in url  # space is encoded, not raw
    assert qs["orderBy"] == ["relevence"]
    assert qs["owned"] == ["yes"]
    assert qs["available"] == ["any"]


def test_search_url_available_only_flag():
    url = _build_search_url("Lib", "x", available_only=True)
    assert parse_qs(urlsplit(url).query)["available"] == ["available"]


def test_sanitize_cookies_normalizes_samesite_and_drops_extras():
    raw = [{"name": "a", "value": "1", "domain": ".x.com", "path": "/",
            "sameSite": "no_restriction", "expires": 1.5, "extra": "drop"}]
    out = _sanitize_cookies(raw)
    assert out[0]["sameSite"] == "None"
    assert out[0]["expires"] == 1
    assert "extra" not in out[0]
