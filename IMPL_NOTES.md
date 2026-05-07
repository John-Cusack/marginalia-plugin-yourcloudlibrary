# Implementation Notes

Live observations from probing YCL on 2026-05-07. These findings drove
the plugin's API-only architecture — they replaced everything the
original PLAN.md guessed about the auth flow, the URL shape, and the
content delivery format.

Probe scripts live in `scripts/probe_*.py`; raw output in `scratch/probe-*.json`.

## URL anatomy

| URL                                                                              | What                                                                  |
| -------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `https://epub.yourcloudlibrary.com/read/{book_id}`                               | The reader URL. **302-redirects** to the detail page, even when authed. |
| `https://ebook.yourcloudlibrary.com/library/{LIBRARY_SLUG}/detail/{book_id}`     | Book detail page. Wrong slug → bounces to marketing.                  |
| `…/detail/{book_id}?_data=routes/library.$name.detail.$id`                       | Remix loader. Returns JSON with the ISBN, title, status, canRead, page count. |
| `https://epubservice.yourcloudlibrary.com/manifest/{ISBN}?catalogName=3m.us`     | Returns a JSON-encoded URL string pointing at the actual manifest.    |
| `https://epubservice.yourcloudlibrary.com/content/{book-uuid}/manifest.json`     | Readium WebPub manifest (standard W3C format).                        |
| `https://epubservice.yourcloudlibrary.com/content/{book-uuid}/{href}`            | Each `readingOrder[i].href` chapter — base64-wrapped XHTML.           |

`{LIBRARY_SLUG}` example: `PalmBeachCountyLibrarySystem`. The slug is
**not** the user's library card number (which the user's `.env` had
called `LIBRARY_ID`); see "Cookies" below for where the slug actually
comes from.

`{book_id}` is opaque (e.g. `onc5689`). The prefix is **not** a library
code — `/library/onc/featured` bounces to marketing. Treat it as an
opaque vendor identifier.

`catalogName=3m.us` is the legacy Bibliotheca/3M backend identifier. It
appears stable for US libraries; non-US may differ — left as a hardcoded
default in `ycl/api/client.py::DEFAULT_CATALOG_NAME`.

## Cookies — the source of truth

After login, the YCL server sets six cookies on `.yourcloudlibrary.com`.
Three carry useful state:

- **`__session_PROD`** — the actual JWT-bearing session cookie (httponly,
  secure, ~30-day expiry).
- **`__config_PROD`** — base64-encoded JSON with library identity:
  ```json
  {
    "library_info": {
      "name": "Palm Beach County Library System",
      "urlName": "PalmBeachCountyLibrarySystem",
      "url": "https://ebook.yourcloudlibrary.com/library/PalmBeachCountyLibrarySystem"
    },
    "library_config": {"reaktor_patron_id": 203592612},
    "login_info": {"barcode": "D027150451", "library": "<UUID>", "state": "FL"}
  }
  ```
  **The cookie body has trailing binary noise after the JSON.** Decoding
  with `rfind('}')` is wrong — that brace may be inside the noise. Use
  `json.JSONDecoder().raw_decode(text[start:])` instead. There's a
  regression test for this in `test_api_cookies.py`.
- **`__mads_PROD`** — patron identity (`patronId`, internal IDs). Not
  currently used by the plugin but worth preserving in the cookie file
  in case future endpoints need it.

The plugin does not require any environment variables for identity. The
slug, library UUID, patron id, and state are all derived from the cookie
at runtime via `ycl.api.cookies.decode_config_cookie`.

## Content format — Readium WebPub, no DRM

The manifest is a standard
[W3C Readium WebPub Manifest](https://readium.org/webpub-manifest/):

```json
{
  "@context": "https://readium.org/webpub-manifest/context.jsonld",
  "metadata": {"title": "...", "identifier": "9780310522744", "author": [...]},
  "readingOrder": [
    {"type": "application/xhtml+xml", "href": "OEBPS/chapter01.xhtml"},
    ...
  ],
  "resources": [...],
  "toc": [...],
  "pageList": [...]
}
```

Each `readingOrder` href is fetched as a separate HTTP GET. The
response body is **base64-encoded XHTML** — superficial obfuscation, not
real DRM. Decoding is one `base64.b64decode` call per chapter.

The XHTML is well-formed (Innodata-generated); Python's stdlib
`html.parser` handles it fine. No need for bs4 or lxml.

## Auth flow — one-time browser, headless thereafter

1. **First-time login** is unavoidable in a browser because each library
   uses its own auth (card+PIN, SSO, library-card-only, etc.). The CLI at
   `ycl/cli/login.py` opens Chromium at `https://www.yourcloudlibrary.com/`
   and polls every 3 s for the YCL session cookies (`__session_PROD` +
   `__config_PROD`) to appear. Once they do, it saves them and exits.
2. **All subsequent operations** are plain async httpx using the saved
   cookies. There's no second browser launch.
3. **When cookies expire** (401, or a 200 that bounced to marketing), the
   client raises `AuthExpiredError` with a hint to re-run the login CLI.
   `ycl.auth_status` reports the same state proactively.

## Performance

End-to-end smoke test (`scripts/probe_auth.py` cookies + new YclClient):

- 27 readingOrder items (cover + frontmatter + 17 chapters + appendices)
- **1.92 seconds** to scrape the full book at concurrency=4
- 484,789 characters of plain text out
- 0 anti-bot challenges, 0 page-turn delays

The old browser-based design would have taken ~10 minutes for the same
book. The API-only path is ~300x faster.

## What is *not* covered yet

- **Audiobooks**. Detail page reports `mediaType: "Audiobook"` for those.
  The manifest endpoint may or may not work for them; not tested.
- **Comics+ / BiblioPlus**. Same library can have multiple sub-services.
  The plugin currently only handles the standard ebook path.
- **Auto-discovery of active loans**. Could call a `/loans` endpoint to
  populate BorrowStore automatically — not implemented.
- **Re-borrow versioning**. Plugin treats re-borrows as new documents
  (Kindle convention); could change to per-borrow versioning.

## Probe scripts

| Script | Purpose |
| --- | --- |
| `scripts/probe_reader.py` | Visit the bare reader URL, dump DOM (run before login). |
| `scripts/probe_portals.py`, `probe_portals2.py` | Test alternative portal URL patterns. |
| `scripts/probe_app.py` | Inspect the post-login Web Patron SPA. |
| `scripts/probe_login_click.py` | Click "Login" and capture the resulting modal. |
| `scripts/probe_auth.py` | Headed: capture all auth-flow traffic. |
| `scripts/probe_reader_passive.py` | Persistent profile, passive observer for reading sessions. |
| `scripts/analyze_capture.py` | Summarize captured network logs. |
