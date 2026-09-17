"""A real core ingestion client on a disposable database, for integration/live tiers.

Everything that touches core lives here, behind ``research-engine>=0.6``:

- The database is ``research_engine.testing.resolve_test_db_url(RE_DB_URL)`` with
  this suite's own scratch database name: the host and credentials you give, never
  the research corpus (unless you set core's explicit opt-out yourself).
- Only the embedder is faked (deterministic vectors); document, text, node,
  passage, embedding, and FTS writes are real.
- Every document the plugin creates is tracked through the client and deleted
  afterwards, and ``CorpusFootprint`` proves the database is back to where it was.

Building the client uses core internals (orchestrator, repositories, and core's
``IngestionServiceAdapter``, the object the loader injects as ``ingestion``) —
this is infrastructure, not an assertion surface. Tests assert through the SDK
client and the plugin's own results.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os

import pytest
import pytest_asyncio

MIN_CORE = (0, 6)


def core_version() -> str | None:
    try:
        return importlib.metadata.version("research-engine")
    except importlib.metadata.PackageNotFoundError:
        return None


def require_core() -> None:
    version = core_version()
    if version is None:
        pytest.skip("research-engine is not installed (pip install research-engine==0.6.0)")
    major, minor = (int(p) for p in version.split(".")[:2])
    if (major, minor) < MIN_CORE:
        pytest.skip(f"research-engine {version} predates the 0.6 plugin contract")


class FakeEmbedder:
    """Deterministic, dependency-free embedding port.

    Core's ``passage_embeddings.embedding`` column has a fixed width (1024), so
    the fake must match it.
    """

    model_name = "ycl-plugin-test-embedder"
    model_version = "1.0"
    dim = 1024

    def _vec(self, text: str) -> list[float]:
        digest = hashlib.shake_256(text.encode("utf-8")).digest(self.dim)
        return [byte / 255.0 for byte in digest]

    async def embed(self, text: str) -> list[float]:
        return self._vec(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


# The plugin suite's own scratch database, so it never shares state (or a
# migration head) with core's test database or anyone's corpus.
TEST_DB_NAME = "research_engine_ycl_plugin_test"


class TrackingIngestion:
    """Pass-through SDK IngestionClient that remembers what it created."""

    def __init__(self, client, scratch, text_repo) -> None:
        self._client = client
        self._scratch = scratch
        self._texts = text_repo
        self.created: list[str] = []

    async def find_existing(self, *, source=None, source_pattern=None):
        return await self._client.find_existing(source=source, source_pattern=source_pattern)

    async def ingest_document(self, **kwargs):
        from uuid import UUID

        result = await self._client.ingest_document(**kwargs)
        if not result.get("skipped"):
            self.created.append(result["document_id"])
            self._scratch.adopt(UUID(result["document_id"]))
        return result

    async def stored_text(self, document_id: str) -> str | None:
        from uuid import UUID

        row = await self._texts.get(UUID(document_id))
        return None if row is None else row.text


def _plugin_registry():
    """A core registry holding this plugin's document types, as loading would."""
    from importlib.resources import files

    import yaml
    from research_engine.plugins.registry import PluginRegistry

    manifest = yaml.safe_load(files("ycl").joinpath("plugin.yaml").read_text(encoding="utf-8"))
    registry = PluginRegistry()
    for document_type in manifest["provides"]["document_types"]:
        spec = {k: v for k, v in document_type.items() if k != "id"}
        registry.register_document_type(document_type["id"], spec, manifest["plugin_id"])
    return registry


@pytest_asyncio.fixture
async def ingestion():
    require_core()
    try:
        from research_engine.adapters.ingestion_client import IngestionServiceAdapter
    except ImportError:
        pytest.skip("core has no IngestionServiceAdapter; update tests/_core_fixtures.py")
    from research_engine.adapters.storage.postgres.engine import build_engine
    from research_engine.adapters.storage.postgres.repositories.document_texts import (
        PGDocumentTextRepo,
    )
    from research_engine.adapters.storage.postgres.repositories.documents import PGDocumentRepo
    from research_engine.adapters.storage.postgres.repositories.nodes import PGDocumentNodeRepo
    from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
    from research_engine.services.ingestion.orchestrator import IngestionOrchestrator
    from research_engine.testing import (
        Corpus,
        CorpusFootprint,
        ensure_test_database,
        resolve_test_db_url,
    )

    db_url = resolve_test_db_url(os.environ.get("RE_DB_URL"), test_db=TEST_DB_NAME)
    if not await ensure_test_database(db_url):
        pytest.skip("No reachable Postgres for the disposable test database (set RE_DB_URL)")
    engine = await build_engine(db_url)
    texts = PGDocumentTextRepo(engine)
    orchestrator = IngestionOrchestrator(
        docs=PGDocumentRepo(engine),
        passages=PGPassageRepo(engine),
        embedding=FakeEmbedder(),
        ingestion_runs=object(),  # unused by find_existing / ingest_drafts
        dispatcher=object(),
        engine=engine,
        document_texts=texts,
        document_nodes=PGDocumentNodeRepo(engine),
    )
    client = IngestionServiceAdapter(orchestrator, _plugin_registry())

    scratch = Corpus(engine)
    before = await CorpusFootprint.measure(engine)
    try:
        yield TrackingIngestion(client, scratch, texts)
    finally:
        try:
            await scratch.cleanup()
            before.assert_unchanged(await CorpusFootprint.measure(engine))
        finally:
            await engine.dispose()
