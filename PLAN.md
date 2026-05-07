# Plan: `marginalia-plugin-yourcloudlibrary`

> **Audience:** another software-architecture reviewer. This document is the
> as-built design after live probing dispelled most of the original
> guesses. See `IMPL_NOTES.md` for raw findings.
>
> **Revision history:**
> - v1: speculative browser-scraping design based on Kindle plugin template.
> - v2: corrected URL patterns and auth selectors after first probe.
> - v3 (this file): API-only architecture after discovering YCL exposes a
>   Readium WebPub manifest with no wire-level DRM.

## Context

YourCloudLibrary (Bibliotheca cloudLibrary) is a public-library ebook
platform. Members of participating libraries borrow ebooks for fixed
durations (typically 14–21 days), after which the loan expires and the
content is no longer accessible. We need a Marginalia AI plugin that
captures borrowed-book text into the corpus before each loan expires,
and tracks expiration state so the user can prioritize captures.

The plugin must:
- Scrape borrowed-book content reliably and quickly.
- Track loan lifecycle (borrowed_at, expires_at, scrape state, ingest state).
- Run unattended after a one-time setup step (no daily UI babysitting).
- Surface loan-expiration warnings via the standard MCP tool surface.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  ycl.cli.login (Playwright headed, one-time per ~30 days)         │
│  → cookies.json on disk                                           │
└──────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  YclClient (httpx async, cookie-authenticated)                    │
│   1. ebook.../detail/{book_id}?_data=...   → ISBN, status, canRead │
│   2. epubservice.../manifest/{ISBN}        → manifest URL         │
│   3. manifest.json                          → readingOrder         │
│   4. each chapter → base64-decode → html.parser → text             │
└──────────────────────────────────────────────────────────────────┘
                          │
        ┌─────────────────┼────────────────────┐
        ▼                 ▼                    ▼
┌──────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│ BorrowStore  │  │ extracted/{slug}/│  │  IngestionClient     │
│ (JSON+flock) │  │   {book_id}.txt  │  │ (ProseWindowChunker  │
│ borrows.json │  │                  │  │  + ingest_drafts)    │
└──────────────┘  └──────────────────┘  └──────────────────────┘
```

**Three subsystems:**

1. **YclClient** (`ycl/api/`) — async httpx client wrapping the YCL
   Readium WebPub flow. Loads cookies from disk on construction; raises
   typed errors (`AuthExpiredError`, `BookNotBorrowedError`,
   `NotAuthenticatedError`, `YclApiError`) for caller flow control.
2. **BorrowStore** (`ycl/borrows.py`) — file-locked JSON registry of
   loans, partitioned by library URL slug (read from the
   `__config_PROD` cookie). Tracks borrowed_at, expires_at, scrape
   state, ingest state, plus `expires_at_is_estimated` so the user
   knows when the value is a guess.
3. **MCP tools** (`ycl/tools/`) — six handlers using the `@tool`
   decorator. Each is a thin orchestration of the API client +
   BorrowStore + IngestionClient. Returns `dict`.

**One-time browser bootstrap:** the only reason Playwright is involved
is that each library has its own bespoke auth flow (card+PIN, SSO,
EZproxy, etc.). The CLI at `ycl/cli/login.py` opens Chromium, polls for
the YCL session cookies, and saves them. After that, everything is
httpx.

## Configuration

Almost zero configuration required — the `__config_PROD` cookie carries
library identity (slug, UUID, state, default loan duration).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `YCL_BORROW_DAYS` | no | `14` | Last-resort fallback when neither an explicit `expires_at` nor a value derivable from the cookie is available. |

Cookies are stored at `~/.marginalia/plugins/yourcloudlibrary/cookies.json`.
Borrow state at `~/.marginalia/plugins/yourcloudlibrary/borrows.json`
(plus `borrows.json.lock` sidecar for `fcntl.flock` serialization).
Extracted text at `extracted/{library_slug}/{book_id}.txt`.

All timestamps are UTC ISO 8601 with `Z` suffix.

## File structure

```
marginalia-plugin-yourcloudlibrary/
├── pack.yaml
├── pyproject.toml
├── README.md
├── PLAN.md
├── IMPL_NOTES.md
├── .env.example
├── ycl/
│   ├── __init__.py
│   ├── _paths.py
│   ├── _config.py
│   ├── _time.py
│   ├── borrows.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── client.py            # YclClient (httpx)
│   │   ├── cookies.py           # __config_PROD decode
│   │   ├── errors.py            # typed errors
│   │   ├── scraper.py           # high-level scrape_book(client, book_id)
│   │   ├── text.py              # XHTML → plain text + base64 decode
│   │   └── types.py             # Book, Manifest, ReadingOrderItem, etc.
│   ├── cli/
│   │   ├── __init__.py
│   │   └── login.py             # one-time interactive login
│   ├── session/
│   │   └── cookies.py           # JSON cookie persistence
│   └── tools/
│       ├── auth_status.py
│       ├── scrape_book.py
│       ├── ingest_book.py
│       ├── check_book.py
│       ├── list_books.py
│       ├── record_borrow.py
│       └── forget_book.py
├── scripts/
│   ├── analyze_capture.py
│   ├── probe_auth.py
│   ├── probe_reader_passive.py
│   └── probe_*.py               # other one-off discovery scripts
└── tests/
    └── unit/
        ├── test_api_client.py     # mocked httpx, full pipeline
        ├── test_api_cookies.py    # __config_PROD decode + jar conversion
        ├── test_api_text.py       # XHTML → text, base64 decode
        ├── test_borrows.py        # store + concurrency
        ├── test_config.py
        └── test_timestamps.py
```

## Component details

### `pack.yaml` — what the harness sees

- `network_allowlist`: `*.yourcloudlibrary.com` (wildcard covers
  ebook./epub./epubservice./service./images.).
- `permissions`: `network: egress`, `subprocess: true`,
  `filesystem: read_write_plugin_data`, `ingest: true`.
- `provides.document_types`: `ycl_book` with `default_chunker: prose_window`.
- `provides.mcp_tools`: 7 tools (auth_status, scrape_book, ingest_book,
  check_book, list_books, record_borrow, forget_book).

### `ycl/api/client.py` — YclClient

```python
class YclClient:
    @classmethod
    def from_cookie_store(cls, path=COOKIE_PATH, *, catalog_name="3m.us") -> YclClient: ...
    async def get_book(self, book_id: str) -> Book: ...
    async def get_manifest(self, isbn: str) -> Manifest: ...
    async def fetch_chapter_text(self, manifest, item) -> str: ...    # raw XHTML
    async def close(self) -> None: ...
```

Uses an `httpx.AsyncClient` with `Origin: https://epub.yourcloudlibrary.com`
and the captured cookies. Translates 401/403 and "200 + bounce-to-marketing"
to `AuthExpiredError`.

### `ycl/api/scraper.py` — high-level orchestrator

```python
async def scrape_book(client: YclClient, book_id: str, *, concurrency: int = 4) -> ScrapeResult:
    book = await client.get_book(book_id)
    assert_borrowed(book)        # raises BookNotBorrowedError if not LOAN+canRead
    manifest = await client.get_manifest(book.isbn)
    # Fan-out: bounded by Semaphore(concurrency); reassemble in order.
    chapters = await asyncio.gather(*[fetch+decode each item])
    return ScrapeResult(book_id, isbn, title, "\n\n".join(chapters), ...)
```

### `ycl/cli/login.py` — one-time browser login

Headed Chromium → `https://www.yourcloudlibrary.com/`. Polls every 3s
for `__config_PROD` and `__session_PROD` cookies on the context. On
detection, saves all cookies to disk and exits. Times out at 15 min.

### MCP tools — handler shape

All use `@tool(id, description, input_schema)` from
`research_engine.plugins.sdk`, return `dict`, accept SDK clients via
`**_clients`. See `ycl/tools/scrape_book.py` for the canonical pattern.

| Tool | Required input | Behavior |
|---|---|---|
| `ycl.auth_status` | — | Reports whether cookies are present and which library. |
| `ycl.scrape_book` | `book_id` | Scrape via API, write `extracted/{slug}/{book_id}.txt`, upsert BorrowStore. Fail-fast on `BookNotBorrowedError`. |
| `ycl.ingest_book` | `book_id` | Idempotent via `find_existing(source=...)`. Re-uses on-disk text unless `rescrape`. Chunks with `ProseWindowChunker`, calls `ingest_drafts(...)`. |
| `ycl.check_book` | `book_id` | Calls live API for current loan status (skippable via `live=false`); merges with BorrowStore + disk + corpus. |
| `ycl.list_books` | — | Iterate BorrowStore; default active-only. |
| `ycl.record_borrow` | `book_id` | Manual record with explicit expires_at. |
| `ycl.forget_book` | `book_id` | Remove BorrowStore entry; corpus passages and on-disk text are kept. |

### Concurrency, idempotency, recovery

- **BorrowStore concurrency:** `fcntl.flock` on a sidecar `.lock` file
  (the data file gets atomically swapped via `os.replace`, which would
  break a flock held on the data file directly). Read paths take
  `LOCK_SH`, write paths take `LOCK_EX`.
- **Idempotency for ingestion:** `find_existing(source=str(text_path.resolve()))`,
  same convention as `marginalia-plugin-kindle`.
- **Re-borrow:** treated as a new document via `force_reingest=True`
  (also Kindle convention). Borrow record is overwritten.
- **Network failures:** httpx retries are not configured by default;
  callers see `YclApiError` and can re-invoke. Chapter fetches are
  parallelized (`Semaphore(4)`) so partial failure of one chapter
  doesn't block the others — but the orchestrator does fail the whole
  scrape if any chapter raises (caller can retry).
- **Auth failures:** `AuthExpiredError` is mapped to a `not_authenticated`
  / `auth_expired` tool result with a hint pointing at `ycl.cli.login`.

### Error model

```
YclApiError
├── NotAuthenticatedError    → no cookies on disk
├── AuthExpiredError         → cookies present but rejected (401, or marketing bounce)
└── BookNotBorrowedError     → loan expired or never existed (status != "LOAN")
```

### Tests

- `test_api_client.py` — mocked httpx via `httpx.MockTransport`, full
  4-step pipeline + auth-expired + book-not-borrowed + 5xx paths.
- `test_api_cookies.py` — `__config_PROD` decode incl. trailing-noise
  regression test (caught a real-world bug).
- `test_api_text.py` — XHTML → text, base64 chapter decode, entity
  handling.
- `test_borrows.py` — JSON roundtrip, library partitioning, expiration
  arithmetic, 16-thread concurrent-write race.
- `test_config.py`, `test_timestamps.py` — env loading + UTC mandate +
  expires_at resolution.

46 tests, run in ~0.2s.

## Verification (end-to-end)

1. `cd /home/john/repos/marginalia-plugin-yourcloudlibrary && uv sync`.
2. `uv run playwright install chromium` (one-time).
3. `uv run python -m ycl.cli.login` — sign in via your library; the
   browser closes automatically once cookies are detected.
4. `ycl.auth_status` should report your library name and slug.
5. Borrow a book in your library's catalog. Copy the `book_id` from
   either the reader URL (`epub.yourcloudlibrary.com/read/{book_id}`)
   or the detail URL.
6. `ycl.scrape_book` with that `book_id`. Expect ~2 seconds for a
   200-page book; check `extracted/{slug}/{book_id}.txt`.
7. `ycl.ingest_book` with the same `book_id`. Verify the returned
   `document_id`.
8. `mcp__research-engine__find_passages` filtered by
   `document_type=ycl_book` — spot-check a passage matches source text.
9. `ycl.check_book` reports `ingested: true`, accurate `expires_at`,
   correct `days_remaining`.
10. `uv run --extra dev pytest` → all green.

## Open questions for the reviewer

1. **Auto-discovery of active loans.** A future `ycl.sync_loans` tool
   could call the YCL "My Loans" page (likely also a Remix loader) and
   auto-populate BorrowStore with accurate `expires_at` values. Worth
   doing in v0.2.
2. **Per-borrow versioning vs duplicate documents.** Currently a
   re-borrow with `force_reingest=True` creates a duplicate. Should we
   instead append to the same document with a borrow-ordinal in
   metadata? Kindle convention is duplicates; this is the same.
3. **Audiobook support.** Detail-page `mediaType` can be `"Audiobook"`.
   The plugin will currently fail at the manifest step for those. The
   transcription/text extraction path is materially different — left
   for a future plugin or a separate code path.
4. **Plugin permissions surface.** `record_borrow` and `forget_book`
   don't need `network` or `ingest`, but the manifest grants them
   plugin-wide. Acceptable given the framework's plugin-wide permission
   model.
5. **`catalogName=3m.us` hardcoding.** Stable for US libraries; non-US
   may differ. If we encounter a non-US user, surface as an env var.
6. **Login re-issuance.** When cookies expire, the plugin currently
   surfaces an error and tells the user to re-run the CLI. Could we
   detect impending expiration and warn ahead of time? Not currently.
