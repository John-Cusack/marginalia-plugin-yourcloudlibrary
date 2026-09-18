# Changelog

All notable changes to this plugin. Versions follow PEP 440.

## 0.3.0 — 2026-09-18

Requires `marginalia-ai` 0.6.x and `marginalia-ai-sdk` 0.6.x. This is a clean
cutover; see "Upgrading from 0.2.x" in the README.

### Changed

- **Package renamed** from `marginalia-plugin-yourcloudlibrary` to
  `marginalia-ai-plugin-yourcloudlibrary`, joining the MarginaliaAI distribution
  family (`marginalia-ai`, `marginalia-ai-sdk`, `marginalia-ai-plugin-*`), with
  complete PyPI metadata (README, Apache-2.0 license expression and file,
  classifiers, keywords, project URLs). Nothing was published under either older
  name, so there is no alias to fall back on. The `ycl` import package, the
  `yourcloudlibrary` plugin id, the `research_engine.plugins` entry-point group and
  the `research-engine-ycl-login` command are unchanged.
- **Entry-point activation.** The wheel advertises
  `research_engine.plugins: yourcloudlibrary = ycl`. Core discovers it and reads the
  manifest without importing the plugin; nothing loads until
  `research-engine plugin enable yourcloudlibrary`.
- **Manifest moved and converted.** Root `pack.yaml` is now `ycl/plugin.yaml`
  (schema v2, `plugin_id: yourcloudlibrary`, `core_api >=0.6,<0.7`) and ships in the
  wheel. Identity, version, and dependencies live only in `pyproject.toml`. Every
  tool's full input schema is in the manifest.
- **Tool ids renamed** from `ycl.*` to `yourcloudlibrary.*`, as manifest v2
  requires tool ids to be namespaced by `plugin_id`. Core publishes them over MCP
  under those ids (and also accepts the `yourcloudlibrary_*` spelling).
  Source-search `ingest_action`s name `yourcloudlibrary.acquire_and_ingest`.
- **SDK cutover.** Every import of `research_engine.plugins.sdk` and
  `research_engine.domain.source_search` now comes from `research_engine_sdk` (the
  import package `marginalia-ai-sdk` ships). No runtime module imports core, and
  the package depends on the SDK, not on the core application.
- **Core does the chunking.** Ingestion calls `IngestionClient.ingest_document()`
  with the canonical text; core applies `ycl_book`'s `prose_window` chunker from the
  manifest. Chapters are sent as sections and become document nodes, replacing the
  per-passage `chapter_index`/`chapter_title` keys the plugin used to write. The
  document source and metadata are unchanged. `yourcloudlibrary.ingest_book` and
  `yourcloudlibrary.acquire_and_ingest` share one ingestion path.
- **Source-search provider contract.** `YclSourceProvider` returns SDK
  `SourceMatch` DTOs and accepts an optional `PluginContext`.
- **Data directory.** State moved from `~/.marginalia/plugins/yourcloudlibrary` to
  `PluginContext.data_dir` (default `~/.research-engine/plugin-data/yourcloudlibrary`,
  following `RE_DATA_DIR`). Tools, the borrow registry, the cookie store, the
  provider, and the login command all resolve that one directory. Books ingested
  under the old path are still recognised as ingested.
- **Session file** is written atomically and owner-only (`0600`).
- `yourcloudlibrary.search_catalog` errors use the standard
  `{"status": "error", "error_type": ...}` envelope, and a missing browser reports
  `browser_unavailable` with the install command.
- Network allowlist narrowed from `*.yourcloudlibrary.com` to the hosts the code
  contacts: `ebook.`, `epubservice.`, and `www.yourcloudlibrary.com`.

### Added

- **`research-engine-ycl-login`** console script replaces
  `python -m ycl.cli.login`. It reports a missing Playwright or Chromium with the
  exact install command, validates the session before saving it, redacts
  secrets from output, accepts `--library` and `--data-dir`, and exits non-zero on
  failure.
- **`research-engine-ycl-login migrate`** copies pre-0.3.0 data after showing every
  source → destination pair. It refuses on conflicting destinations, verifies
  copies and reads the migrated session and registry back, and deletes the
  originals only when asked.
- Test tiers: unit (SDK + plugin), contract (manifest, entry point, schemas, wheel and
  sdist contents), integration (core 0.6 + a disposable database), and opt-in live.

### Removed

- Manifest `requires.pip` and `requires.setup_commands`. Installing or enabling the
  plugin never downloads a browser; run `python -m playwright install chromium`
  yourself.
- The `integration` extra and its path dependency on a core checkout.

## 0.2.0 — 2026-06-30

### Added

- **Whole-catalog search.** `ycl.search_catalog` now searches the library's entire
  catalog (not just loans) with relevance ranking and live availability
  (`currently_available`, `total_copies`, `is_pay_per_use`). Results carry the
  catalog `documentId`, which is the id every other tool accepts.
- **`search_sources` provider.** `YclSourceProvider` contributes catalog matches to
  core's cross-library discovery. Availability maps to `borrowable` (with
  `hold_required` when every copy is out), `purchasable` for pay-per-use, and
  `external_only` otherwise. Each match carries a `ycl.acquire_and_ingest` action.
- **`ycl.acquire_and_ingest`.** Borrows a catalog book, scrapes and ingests it, then
  returns the loan by default so the slot can be reused. A book that was already on
  loan is never returned. The loan it opened is returned even when ingest fails or is
  cancelled.
- `YclClient.borrow()` / `YclClient.return_book()` through the detail loader.
- `ycl.auth_status` reports `can_read`, `reading_status`, and `reading_hint`. The
  reading cookie (`__session_PROD`) lasts about a day and `epubservice` enforces it,
  while the catalog keeps working on a stale session.
- Opt-in integration suite (`-m integration`) against a scratch Postgres and a live
  session; borrow/return tests also need `YCL_LIVE_ACQUIRE=1`.

### Changed

- Catalog search runs in a warmed headless Chromium context. Measured: plain httpx
  and cold browser requests get empty results; only a context that has loaded a
  catalog page gets items. Playwright is therefore used at runtime, not only for login.

### Removed

- The httpx-only catalog search added in the P0 work. It returned no results against
  the live site and keyed hits on an id the detail route rejects.

## 0.1.0

- API-only scraping via the Readium WebPub endpoints, one-time browser login, borrow
  lifecycle store, loan sync with real due dates, author/subject capture, retries and
  partial-scrape tolerance, and chapter-aware ingestion.
