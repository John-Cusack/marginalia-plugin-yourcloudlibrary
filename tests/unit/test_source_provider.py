"""Unit tests for SourceMatch availability mapping (needs the host SDK types)."""

from __future__ import annotations

import pytest

# Needs the real host domain types; tests/conftest.py's stub doesn't provide them.
pytest.importorskip(
    "research_engine.domain.source_search", reason="install the 'integration' extra"
)

from research_engine.domain.source_search import Availability  # noqa: E402

from ycl.api.catalog import CatalogItem  # noqa: E402
from ycl.source_provider import _availability, item_to_match  # noqa: E402


def _item(**over) -> CatalogItem:
    base = dict(
        document_id="d1", isbn="i", title="t", total_copies=1,
        currently_available=1, is_pay_per_use=False, matching_score=10.0,
    )
    base.update(over)
    return CatalogItem(**base)


def test_pay_per_use_is_purchasable_even_when_available():
    # Regression: a PPU title with copies available must NOT be 'borrowable'.
    av, extra = _availability(_item(is_pay_per_use=True, currently_available=3))
    assert av is Availability.purchasable
    assert "hold_required" not in extra


def test_available_copy_is_borrowable():
    av, extra = _availability(_item(currently_available=2, total_copies=2))
    assert av is Availability.borrowable
    assert "hold_required" not in extra


def test_owned_but_none_available_is_borrowable_with_hold():
    av, extra = _availability(_item(currently_available=0, total_copies=4))
    assert av is Availability.borrowable
    assert extra.get("hold_required") is True


def test_not_owned_is_external_only():
    av, _ = _availability(_item(currently_available=0, total_copies=0))
    assert av is Availability.external_only


def test_item_to_match_carries_documentid_and_action():
    m = item_to_match(_item(document_id="onc5689"), max_score=10.0)
    assert m.source_id == "onc5689"
    assert m.ingest_action.tool == "ycl.acquire_and_ingest"
    assert m.ingest_action.args["book_id"] == "onc5689"
    assert m.metadata["corpus_source_pattern"] == "onc5689"
