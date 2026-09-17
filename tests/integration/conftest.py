"""Integration-test fixtures: a REAL ingestion client + a live YCL session guard.

Runs against the host engine (``research_engine``, pulled in editable via the
``integration`` extra) and a real Postgres. The DB write is genuine; only the
embedder is faked (deterministic vectors) so we don't call a real embedding model.

Run:  uv run --extra dev --extra integration python -m pytest -m integration
Needs: a logged-in YCL session (``python -m ycl.cli.login``) and a reachable
Postgres (the dev one at localhost:5435, or ``RE_DB_URL``). Tests skip cleanly
when either is missing.
"""

from __future__ import annotations

import hashlib
import os

import pytest
import pytest_asyncio

DEFAULT_DB_URL = "postgresql+asyncpg://re_dev:re_dev_pass@localhost:5435/research_engine"


def _db_url() -> str:
    """The database these tests may write to — never the real corpus.

    This suite runs the *true* ingest path: real document, passage, embedding
    and FTS writes. Pointed at the dev database it does not test ingestion, it
    performs it — which is how 12 borrowed books and 2,095 stub embeddings ended
    up in a live corpus, leaving those passages invisible to semantic search
    because the stub embedder is not the search model.

    `resolve_test_db_url` redirects the database name and keeps everything else,
    so this reaches a scratch database by default. Set
    RE_TEST_ALLOW_REAL_CORPUS=1 to opt out, deliberately.
    """
    from research_engine.testing import resolve_test_db_url

    return resolve_test_db_url(os.environ.get("RE_DB_URL", DEFAULT_DB_URL))


class FakeEmbedder:
    """Deterministic, dependency-free EmbeddingPort impl.

    ``core.passage_embeddings.embedding`` is an unconstrained ``vector`` with no
    fixed-dim index, so a small dim is fine and fast. Same text → same vector, so
    ingestion is reproducible.
    """

    model_name = "fake-test-embedder"
    model_version = "1.0"
    dim = 8

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [h[i] / 255.0 for i in range(self.dim)]

    async def embed(self, text: str) -> list[float]:
        return self._vec(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


@pytest_asyncio.fixture
async def ingestion():
    """A real ``IngestionOrchestrator`` backed by the test Postgres + FakeEmbedder.

    Matches exactly what the plugin host injects as the ``ingestion`` client, so
    the tool handlers run their true ingest path (real doc/passage/embedding/FTS
    writes). Skips if the engine or DB is unreachable.
    """
    pytest.importorskip("research_engine", reason="install the 'integration' extra")
    from research_engine.adapters.storage.postgres.engine import build_engine
    from research_engine.adapters.storage.postgres.repositories.documents import PGDocumentRepo
    from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
    from research_engine.services.ingestion.orchestrator import IngestionOrchestrator
    from research_engine.testing import Corpus, CorpusFootprint, ensure_test_database

    db_url = _db_url()
    if not await ensure_test_database(db_url):
        pytest.skip(f"No reachable test Postgres at {db_url}")
    try:
        engine = await build_engine(db_url)
        async with engine.begin() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — any connectivity failure → skip
        pytest.skip(f"No reachable test Postgres at {db_url}: {exc}")

    orchestrator = IngestionOrchestrator(
        docs=PGDocumentRepo(engine),
        passages=PGPassageRepo(engine),
        embedding=FakeEmbedder(),
        ingestion_runs=object(),   # unused by find_existing / ingest_drafts
        dispatcher=object(),       # unused by find_existing / ingest_drafts
        engine=engine,
    )
    # Track what the ingest under test creates, and remove it afterwards. The
    # footprint check is the backstop: if anything is left behind, the suite
    # fails rather than quietly growing the database.
    scratch = Corpus(engine)
    orchestrator._scratch_corpus = scratch  # noqa: SLF001 — tests adopt ids via this
    before = await CorpusFootprint.measure(engine)
    try:
        yield orchestrator
    finally:
        try:
            await _adopt_documents_created_during(engine, scratch, before)
            await scratch.cleanup()
            before.assert_unchanged(await CorpusFootprint.measure(engine))
        finally:
            await engine.dispose()


async def _adopt_documents_created_during(engine, scratch, before) -> None:
    """Claim every document that appeared while the test ran.

    The handlers create documents themselves, so the fixture cannot know their
    ids up front. Anything newer than the pre-test high-water mark belongs to
    this test.
    """
    import sqlalchemy as sa
    from research_engine.adapters.storage.postgres.schema import documents

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.select(documents.c.id).order_by(documents.c.ingested_at.desc()).limit(
                    max(0, (await conn.execute(
                        sa.select(sa.func.count()).select_from(documents)
                    )).scalar_one() - before.documents)
                )
            )
        ).all()
    for row in rows:
        scratch.adopt(row[0])


@pytest.fixture
def ycl_session():
    """Skip unless an unexpired YCL catalog session is on disk.

    The catalog needs ``__config_PROD``; a browser context silently drops an
    expired cookie, so a stale file would fail as "not authenticated" against
    the live site rather than skip.
    """
    import time

    from ycl._paths import COOKIE_PATH
    from ycl.api.cookies import cookie_expiry
    from ycl.session.cookies import CookieStore

    cookies = CookieStore(COOKIE_PATH).load()
    if not cookies:
        pytest.skip("No YCL session cookies — run `python -m ycl.cli.login` once.")
    expires = cookie_expiry(cookies, "__config_PROD")
    if expires is not None and expires < time.time():
        pytest.skip("YCL catalog session expired — re-run `python -m ycl.cli.login`.")


@pytest.fixture
def ycl_can_read():
    """Skip unless the session can actually fetch book content.

    Reading/scrape needs an unexpired ``__session_PROD`` (epubservice enforces it
    strictly). On a stale session we SKIP — not fail — since the fix is a user
    re-login, not a code bug.
    """
    from ycl._paths import COOKIE_PATH
    from ycl.api.cookies import reading_session_status
    from ycl.session.cookies import CookieStore

    cookies = CookieStore(COOKIE_PATH).load()
    if not cookies:
        pytest.skip("No YCL session cookies — run `python -m ycl.cli.login`.")
    status = reading_session_status(cookies)
    if not status["ok"]:
        pytest.skip(f"YCL reading session unavailable ({status['reason']}) — re-run login.")
