# Changelog

All notable changes to this plugin. Versions follow PEP 440.

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
