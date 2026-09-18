# marginalia-ai-plugin-yourcloudlibrary

A [Research Engine](https://github.com/John-Cusack/MarginaliaAI) plugin for
YourCloudLibrary (Bibliotheca cloudLibrary). It searches your library's catalog,
contributes matches to Research Engine's cross-source `search_sources`, and ingests
books you borrow — optionally borrowing a book, ingesting it, and returning the
loan in one step.

> **Borrowed books are licensed content.** The plugin stores the text of books you
> borrow in your own corpus and plugin data directory for your personal research.
> Do not publish, share, or redistribute extracted text, and follow your library's
> terms of use.

## Compatibility

| | |
|---|---|
| Research Engine | `marginalia-ai` 0.6.x (`core_api >=0.6,<0.7`) |
| SDK | `marginalia-ai-sdk` 0.6.x (installed automatically) |
| Python | 3.11+ |
| OS | Linux or macOS (the borrow registry uses POSIX file locks) |
| Account | A library card at a library that uses YourCloudLibrary |

## Install

Install into the same environment as Research Engine:

```bash
python -m pip install marginalia-ai-plugin-yourcloudlibrary
# or, if Research Engine was installed with pipx:
pipx inject marginalia-ai marginalia-ai-plugin-yourcloudlibrary
```

Then install the browser Playwright drives. Installing the wheel never downloads it:

```bash
python -m playwright install chromium
# pipx: use the interpreter inside Research Engine's pipx environment
"$(pipx environment --value PIPX_LOCAL_VENVS)/marginalia-ai/bin/python" -m playwright install chromium
```

Use the same environment's Python: each Playwright release expects its own
Chromium build.

**Why Playwright is a regular dependency, not an extra:** login needs a real
browser, and so does catalog search. The catalog's relevance search only returns
results inside a browser context that has loaded a catalog page; plain HTTP and
cold browser requests come back empty (measured; see `IMPL_NOTES.md`). Scraping,
loans, and borrow/return are plain HTTP.

## Log in

```bash
research-engine-ycl-login
```

A visible Chromium window opens. Sign in the way you normally do (card and PIN,
SSO, …). When the session cookies appear, the command checks they decode to a
library, saves them owner-only (`0600`) in the plugin data directory, closes the
browser, and exits `0`. It gives you 15 minutes and exits non-zero if you close the
window or time runs out; nothing is saved in that case. It never asks for your
password on the terminal and never prints cookie values.

Options:

- `--library URL_NAME` — start on your library's catalog page (for example
  `--library PalmBeachCountyLibrarySystem`). After the first login the saved
  library is used automatically.
- `--data-dir DIR` — only if Research Engine's data directory is not the default
  or `RE_DATA_DIR` (see [Configuration](#configuration)).

**Re-run it when reading stops working.** The reading cookie lasts about a day.
Catalog search and borrowing keep working on a stale session, but fetching book
content does not, so `yourcloudlibrary.auth_status` reports `can_read: false` and
`yourcloudlibrary.acquire_and_ingest` refuses to borrow until you log in again.

If the command says Playwright or Chromium is missing, it prints the exact install
command to run.

## Enable in Research Engine

Installing makes the plugin *available*; nothing runs until you approve it:

```bash
research-engine plugin list                     # yourcloudlibrary: available
research-engine plugin audit yourcloudlibrary   # permissions + contributions, no import
research-engine plugin enable yourcloudlibrary  # confirm (non-interactive: --yes)
```

Restart `research-engine serve` afterwards. After an upgrade that changes the
version or manifest, core marks the plugin pending until you run
`research-engine plugin approve-upgrade yourcloudlibrary`.

If `research-engine plugin list` stops with "Local embedding support is not
installed" or "Local reranking support is not installed", that is core building
its inference stack, which a base install has no models for — it is not about this
plugin. Either install `marginalia-ai[local-inference]`, or point core at a
remote inference server (`RE_EMBEDDING_PROVIDER=remote_api`,
`RE_INFERENCE_BASE_URL=…`, and `RE_RERANKER_PROVIDER=remote_api` or `none`).

### What you are approving

| Permission | Why |
|---|---|
| `network: egress` to `ebook.yourcloudlibrary.com`, `epubservice.yourcloudlibrary.com`, `www.yourcloudlibrary.com` | Book details, loans, borrow/return and catalog search (`ebook.`); manifests and chapter content (`epubservice.`); the login start page (`www.`). |
| `subprocess` | Chromium for login and catalog search. That browser also loads page resources and your library's sign-in provider, which Research Engine does not mediate. |
| `filesystem: plugin_data` | Session cookies, the borrow registry, and extracted text in the plugin data directory. |
| `ingest` | Adding books to your corpus as `ycl_book` documents. |

**An enabled plugin is trusted code running inside the Research Engine process.**
Permissions scope the clients core hands it; they are not a sandbox. Only enable
releases you trust.

## Use it

The plugin contributes:

| Tool | What it does |
|---|---|
| `yourcloudlibrary.search_catalog` | Relevance search over the whole catalog with live availability. Returns each book's `book_id` (catalog `documentId`). Read-only. |
| `yourcloudlibrary.acquire_and_ingest` | Borrow a book, scrape and ingest it, then return the loan (default) so the slot is free again. Never returns a loan you already had; a failed return is reported and never undoes the ingest. |
| `yourcloudlibrary.ingest_book` | Ingest a book you have on loan (scrapes, or reuses the on-disk copy). Idempotent. |
| `yourcloudlibrary.scrape_book` | Save a borrowed book's text to disk without ingesting. |
| `yourcloudlibrary.sync_loans` | Pull your active loans and their real due dates into the borrow registry. |
| `yourcloudlibrary.list_books` | Loans the plugin knows about (active by default). |
| `yourcloudlibrary.check_book` | Loan, disk, and corpus state for one book. |
| `yourcloudlibrary.record_borrow` / `yourcloudlibrary.forget_book` | Add or remove a registry entry by hand. `forget_book` never deletes corpus data or extracted text. |
| `yourcloudlibrary.auth_status` | Whether a session exists, which library, and whether it can still read. |

It also registers the `ycl_book` document type (chunked by core with
`prose_window`, one document node per chapter) and a `search_sources` provider, so
core's cross-source discovery includes catalog matches, each with a
`yourcloudlibrary.acquire_and_ingest` action. The provider warms its browser in the
background: the first discovery round after startup contributes nothing, later
rounds do.

Typical flows:

- **Find and ingest:** `search_sources` or `yourcloudlibrary.search_catalog` →
  `yourcloudlibrary.acquire_and_ingest` with the returned `book_id`.
- **Books you borrowed in the app:** `yourcloudlibrary.sync_loans` →
  `yourcloudlibrary.ingest_book` for each loan before it expires.

## Upgrading from 0.2.x

0.3.0 is a clean cutover:

1. Remove the old package or pack (`marginalia-plugin-yourcloudlibrary`) and install
   this one as above.
2. **Tool ids are namespaced by plugin id:** `ycl.<name>` is now
   `yourcloudlibrary.<name>`. Update saved prompts or scripts that call tools by
   name. (Core also accepts the underscored spelling, `yourcloudlibrary_<name>`.)
3. Move your data from `~/.marginalia/plugins/yourcloudlibrary`:

   ```bash
   research-engine-ycl-login migrate            # shows every source → destination, asks first
   research-engine-ycl-login migrate --yes      # non-interactive copy
   research-engine-ycl-login migrate --yes --remove-old   # copy, verify, then delete originals
   ```

   It copies cookies, the borrow registry, extracted texts, chapter sidecars, and
   partial scrapes; refuses (copying nothing) if a destination file already exists
   with different content; keeps the cookie file owner-only; verifies every copy
   byte-for-byte and reads the migrated session and registry back before it offers
   to delete anything. Books already ingested from the old location are recognised
   and not ingested twice.

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| Plugin data directory | `~/.research-engine/plugin-data/yourcloudlibrary/` | Passed by core as `PluginContext.data_dir`. Follows `RE_DATA_DIR` (`$RE_DATA_DIR/plugin-data/yourcloudlibrary`). If core's data directory is set only in its `.env` file, pass `--data-dir` to `research-engine-ycl-login`. |
| `YCL_BORROW_DAYS` | `14` | Loan length assumed when neither the library nor the caller supplies a due date. |

Data directory layout:

```text
cookies.json                                 # session (0600) — treat like a password
borrows.json, borrows.json.lock              # loan registry, by library
extracted/<library>/<book_id>.txt            # canonical text (licensed content)
extracted/<library>/<book_id>.chapters.json  # chapter structure for re-ingest
```

## Disable and uninstall

```bash
research-engine plugin disable yourcloudlibrary
python -m pip uninstall marginalia-ai-plugin-yourcloudlibrary
```

Neither step deletes anything you created: ingested documents stay in your
corpus, and the plugin data directory (session, registry, extracted text) stays
on disk. Research Engine keeps the approval record and lists the plugin as missing
until you run `research-engine plugin forget yourcloudlibrary`. To remove your data
too, delete the plugin data directory yourself; to remove ingested books, delete
the `ycl_book` documents in Research Engine.

## Security and support

- Report bugs at
  <https://github.com/John-Cusack/marginalia-plugin-yourcloudlibrary/issues>.
  Never paste `cookies.json`, cookie values, or extracted book text into an issue.
- For a security problem, open an issue that asks for a private contact and leave
  the details out of it.
- Your library session grants access to your account, including borrowing. Keep
  the plugin data directory private.

## Development

```bash
uv sync --extra dev
uv run ruff check ycl tests
uv run pytest                       # unit + contract (no network, no core)
uv run pytest -m integration        # needs marginalia-ai==0.6.0 and RE_DB_URL
uv run pytest -m live               # real site, your saved session
```

- **Unit** tests use only the SDK and the plugin. Until `marginalia-ai-sdk` 0.6 is
  on PyPI they run against a test-only stand-in in `tests/_sdk_standin`; the pytest
  header says which SDK is in use, and `YCL_TEST_REQUIRE_REAL_SDK=1` makes the
  stand-in an error.
- **Contract** tests check `ycl/plugin.yaml` against the SDK model and the code,
  that no runtime module imports core, and build the wheel and sdist to inspect
  what would be published.
- **Integration** tests run only where `marginalia-ai==0.6.0` is installed. They create a scratch database named `research_engine_ycl_plugin_test`
  on the server in `RE_DB_URL`, ingest a generated fixture book, and delete exactly
  what they created. The CLI lifecycle test also needs this plugin installed as a
  wheel and `research-engine` on `PATH`.
- **Live** tests read your saved session, copy it into a temp data directory, and
  never borrow unless `YCL_LIVE_ACQUIRE=1`.

`IMPL_NOTES.md` records the live-traffic findings behind the client; `scripts/`
holds the probes that produced them.
