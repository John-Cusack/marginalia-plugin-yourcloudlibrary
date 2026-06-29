# marginalia-plugin-yourcloudlibrary

YourCloudLibrary (Bibliotheca cloudLibrary) borrowed-book scraper and
ingestion plugin for Marginalia AI.

Scrapes borrowed library ebooks via the YCL Readium WebPub API
(`epubservice.yourcloudlibrary.com`) using session cookies captured in a
one-time browser login. After the initial login, every subsequent operation
is plain async httpx — no headless browser, no page-turn loop. A full 200-
page book scrapes in ~2 seconds.

## Setup

```bash
uv sync
uv run playwright install chromium      # only used by the one-time login
uv run python -m ycl.cli.login          # opens a Chromium window; sign in
```

The login script opens the YCL marketing page, you complete your library's
normal login flow, the script auto-detects the YCL session cookies and
saves them to `~/.marginalia/plugins/yourcloudlibrary/cookies.json`. From
that point forward, all the MCP tools work via plain httpx with no
interactive UI.

Re-run `python -m ycl.cli.login` whenever the session expires (typically
every 30 days, or sooner if your library forces re-auth).

## How it actually works

```
                              ┌──────────────────────────────────────┐
                              │  ycl.cli.login (Playwright, one-time) │
                              │  → cookies.json on disk               │
                              └──────────────────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  YclClient (httpx + cookies)                                        │
│   1. GET  ebook.../detail/{book_id}?_data=routes/...    → ISBN       │
│   2. GET  epubservice.../manifest/{ISBN}?catalogName=3m.us  → URL    │
│   3. GET  manifest.json                                  → readingOrder │
│   4. GET  each chapter, base64-decode, html.parser → plain text       │
└─────────────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
                          ┌──────────────────────────────────────┐
                          │  ProseWindowChunker → ingest_drafts  │
                          └──────────────────────────────────────┘
```

The book content is delivered as a Readium WebPub manifest — standard
W3C format — with each chapter as base64-wrapped XHTML. There's no DRM
at the wire layer. See `IMPL_NOTES.md` for the live-traffic findings
that drove this design.

## Configuration

Almost no config required — most identity (library slug, library UUID,
patron id) is read from the `__config_PROD` cookie at runtime.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `YCL_BORROW_DAYS` | no | `14` | Last-resort fallback when no `expires_at` is supplied. Most libraries use 14 or 21 days. |

## MCP tools

- `ycl.auth_status` — report whether session cookies are present and which library.
- `ycl.scrape_book` — fetch a borrowed book's full text and save to disk.
- `ycl.ingest_book` — fetch (if needed) and ingest into the corpus as `ycl_book`.
- `ycl.check_book` — borrow + disk + corpus state for one book; calls the live API by default.
- `ycl.list_books` — list known borrows for the current library.
- `ycl.record_borrow` — register a loan without scraping (for queueing).
- `ycl.forget_book` — remove a borrow record (corpus passages are kept).

## Storage

```
~/.marginalia/plugins/yourcloudlibrary/
├── borrows.json                       # loan registry, partitioned by library urlName
├── borrows.json.lock                  # fcntl lock sidecar
├── cookies.json                       # YCL session cookies (re-issued by ycl.cli.login)
└── extracted/{library_slug}/
    ├── {book_id}.txt                   # canonical plain-text extract
    └── {book_id}.chapters.json         # chapter structure sidecar (title + toc per chapter)
```

The `.chapters.json` sidecar records the book title and each chapter's
`index`/`href`/`title`/`length`, so a re-ingest from the on-disk cache rebuilds
the same per-chapter passage metadata (`chapter_index`/`chapter_title`) a fresh
scrape produces — without a network round-trip. Books scraped before the
sidecar existed simply fall back to flat-text chunking.

All timestamps are stored as UTC ISO 8601 with `Z` suffix.

## Development

```bash
uv run --extra dev pytest               # unit tests, no network
uv run python -m ycl.cli.login           # one-time browser login
```

Probe scripts (under `scripts/`) capture and analyze YCL traffic. See
`IMPL_NOTES.md` for the live-traffic findings that drove the API-only
architecture, and `PLAN.md` for the broader design notes.
