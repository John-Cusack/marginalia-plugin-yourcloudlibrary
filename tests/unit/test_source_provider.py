"""YclSourceProvider against the SDK source-search DTOs (no browser, no network)."""

from __future__ import annotations

from research_engine_sdk import Availability, SourceMatch, SourceQuery, SourceSearchProvider

from ycl.api.catalog import CatalogItem
from ycl.session.cookies import CookieStore
from ycl.source_provider import YclSourceProvider, _availability, item_to_match


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


def test_item_to_match_is_an_sdk_dto_with_documentid_and_action():
    m = item_to_match(_item(document_id="onc5689", matching_score=5.0), max_score=10.0)
    assert isinstance(m, SourceMatch)
    assert m.plugin == "yourcloudlibrary"
    assert m.source_id == "onc5689"
    assert m.confidence == 0.5
    assert m.ingest_action.tool == "yourcloudlibrary.acquire_and_ingest"
    assert m.ingest_action.args == {"book_id": "onc5689"}
    assert m.metadata["corpus_source_pattern"] == "onc5689"
    # Round-trips through the SDK model, as core will when merging providers.
    assert SourceMatch.model_validate(m.model_dump()) == m


def test_provider_satisfies_sdk_protocol_and_uses_context_data_dir(plugin_context, plugin_paths):
    provider = YclSourceProvider(plugin_context)
    assert isinstance(provider, SourceSearchProvider)
    assert provider._cookie_path == plugin_paths.cookie_path


async def test_cold_search_warms_in_background_and_returns_nothing(plugin_context, monkeypatch):
    provider = YclSourceProvider(plugin_context)
    started = []
    monkeypatch.setattr(provider._searcher, "ensure_warming", lambda: started.append(True))

    assert await provider.search(SourceQuery(query="augustine"), limit=5) == []
    assert started == [True]


async def test_warm_search_maps_items(plugin_context, monkeypatch):
    provider = YclSourceProvider(plugin_context)
    provider._searcher._warm = True
    queries = []

    async def fake_search(terms, *, limit):
        queries.append((terms, limit))
        return [_item(document_id="a", matching_score=8.0), _item(document_id="b", matching_score=4.0)]

    monkeypatch.setattr(provider._searcher, "search", fake_search)

    matches = await provider.search(
        SourceQuery(query="ignored", title="Confessions", author="Augustine"), limit=5
    )

    assert queries == [("Confessions Augustine", 5)]
    assert [(m.source_id, m.confidence) for m in matches] == [("a", 1.0), ("b", 0.5)]


async def test_search_failure_never_poisons_the_fanout(plugin_context, monkeypatch):
    provider = YclSourceProvider(plugin_context)
    provider._searcher._warm = True

    async def broken(terms, *, limit):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(provider._searcher, "search", broken)
    assert await provider.search(SourceQuery(query="x"), limit=5) == []


async def test_healthcheck_reads_the_context_session(plugin_context, plugin_paths):
    provider = YclSourceProvider(plugin_context)
    assert (await provider.healthcheck())["status"] == "unconfigured"

    CookieStore(plugin_paths.cookie_path).save([{"name": "other", "value": "x"}])
    assert (await provider.healthcheck())["status"] == "unauthenticated"
