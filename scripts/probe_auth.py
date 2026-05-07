"""Capture YCL auth + reader traffic during a real login.

Usage:
    uv run python scripts/probe_auth.py <LIBRARY_ID> [<BOOK_ID>]

What it does:
    1. Launches a headed Chromium window.
    2. Navigates to https://ebook.yourcloudlibrary.com/library/<LIBRARY_ID>/featured.
    3. You log in manually in the visible window.
    4. The script polls for the "My Books" nav button to confirm authed state,
       then snapshots cookies, localStorage, sessionStorage, and the full
       request/response log to scratch/auth-capture/.
    5. If you passed a BOOK_ID, it then navigates to the reader URL and
       records that traffic separately so we can see how book content is
       fetched (REST? streaming? DRM blob?).

Everything is saved under scratch/auth-capture/ so the analysis can run
offline. Nothing is uploaded anywhere.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

OUTPUT_DIR = Path("scratch/auth-capture")
LOGIN_TIMEOUT_SECONDS = 600  # 10 minutes is plenty for SSO redirects


def _is_yourcloudlibrary(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.endswith("yourcloudlibrary.com")


async def _record_request_responses(page, log: list[dict]) -> None:
    """Hook page events to record all requests + responses."""

    def on_request(req):
        log.append(
            {
                "phase": "request",
                "ts": asyncio.get_event_loop().time(),
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "headers": dict(req.headers),
                "post_data": req.post_data,
            }
        )

    async def on_response(resp):
        try:
            req = resp.request
            entry: dict = {
                "phase": "response",
                "ts": asyncio.get_event_loop().time(),
                "method": req.method,
                "url": resp.url,
                "status": resp.status,
                "resource_type": req.resource_type,
                "headers": dict(resp.headers),
            }
            content_type = (resp.headers.get("content-type") or "").lower()
            # Record body for JSON / small text responses on YCL hosts.
            if (
                _is_yourcloudlibrary(resp.url)
                and (
                    "json" in content_type
                    or "text" in content_type
                    or "xml" in content_type
                )
                and req.resource_type in ("xhr", "fetch", "document")
            ):
                try:
                    body = await resp.text()
                    if len(body) < 200_000:
                        entry["body"] = body
                    else:
                        entry["body_truncated_at"] = 200_000
                        entry["body"] = body[:200_000]
                except Exception as exc:
                    entry["body_error"] = str(exc)
            log.append(entry)
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", lambda r: asyncio.create_task(on_response(r)))


async def _wait_for_authed(page, *, timeout_seconds: int) -> bool:
    """Poll for the 'My Books' button — that's the post-auth signal."""
    poll_interval = 2
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            count = await page.get_by_role("button", name="My Books").count()
            if count > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return False


async def _snapshot_state(page, context, *, label: str) -> dict:
    snapshot: dict = {"label": label, "url": page.url, "title": await page.title()}
    snapshot["cookies"] = await context.cookies()
    try:
        snapshot["localStorage"] = await page.evaluate(
            "() => Object.fromEntries(Object.entries(localStorage))"
        )
    except Exception as exc:
        snapshot["localStorage_error"] = str(exc)
    try:
        snapshot["sessionStorage"] = await page.evaluate(
            "() => Object.fromEntries(Object.entries(sessionStorage))"
        )
    except Exception as exc:
        snapshot["sessionStorage_error"] = str(exc)
    return snapshot


async def _reader_url_is_authed(page, book_id: str) -> bool:
    """Probe the reader URL: if it doesn't bounce to marketing, we're authed."""
    try:
        await page.goto(
            f"https://epub.yourcloudlibrary.com/read/{book_id}",
            wait_until="domcontentloaded",
            timeout=15000,
        )
    except Exception:
        return False
    await asyncio.sleep(2)
    final = (page.url or "").lower()
    return (
        "yourcloudlibrary.com/en/home" not in final
        and "epub.yourcloudlibrary.com/read" in final
    )


async def main() -> None:
    book_id = sys.argv[1] if len(sys.argv) > 1 else "onc5689"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    network_log: list[dict] = []

    async with async_playwright() as pw:
        # Headed so the user can complete login. Use a regular context
        # (not persistent) — we capture the cookies explicitly at the end.
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        await _record_request_responses(page, network_log)

        # Open the reader URL directly. Unauth'd, it bounces to marketing
        # — that's expected. The user then navigates to their library's
        # login page (using the "Find your library" widget on the marketing
        # site, or a bookmark) and signs in. We poll the reader URL on a
        # background timer; when it stops bouncing, we've got cookies.
        reader_url = f"https://epub.yourcloudlibrary.com/read/{book_id}"
        print(f"\n=== Opening reader: {reader_url} ===", flush=True)
        print(
            "It will redirect to the YCL marketing page initially — that's\n"
            "fine. From there, click 'Find your library' (or open your\n"
            "library's website), sign in with your card + PIN as you\n"
            "normally would. I will keep polling and detect when you're\n"
            f"authenticated (up to {LOGIN_TIMEOUT_SECONDS // 60} min).\n",
            flush=True,
        )
        try:
            await page.goto(reader_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            print(f"initial goto failed: {exc}", file=sys.stderr)

        await asyncio.sleep(3)

        # Save what the bounce looks like before login completes.
        (OUTPUT_DIR / "network-pre-login.json").write_text(
            json.dumps(network_log, indent=2)
        )

        print("Waiting for login (probing reader URL every 30s)...", flush=True)
        authed = False
        elapsed = 0
        while elapsed < LOGIN_TIMEOUT_SECONDS:
            await asyncio.sleep(30)
            elapsed += 30
            # Open a side-channel page so we don't hijack what the user is
            # doing in the main tab.
            probe_page = await context.new_page()
            try:
                if await _reader_url_is_authed(probe_page, book_id):
                    authed = True
                    await probe_page.close()
                    break
            finally:
                try:
                    await probe_page.close()
                except Exception:
                    pass
            print(f"  ...still waiting ({elapsed}s)", flush=True)

        if not authed:
            print(
                f"\nTIMEOUT: reader URL still bouncing after "
                f"{LOGIN_TIMEOUT_SECONDS}s. Saving partial capture.\n",
                file=sys.stderr,
            )

        print("Snapshotting authed state...", flush=True)
        post_login_state = await _snapshot_state(page, context, label="post_login")
        (OUTPUT_DIR / "state-post-login.json").write_text(
            json.dumps(post_login_state, indent=2, default=str)
        )

        # Split network log: everything up to here is "auth phase".
        (OUTPUT_DIR / "network-auth.json").write_text(
            json.dumps(network_log, indent=2)
        )
        print(
            f"  → saved {len(network_log)} network entries to "
            f"network-auth.json",
            flush=True,
        )

        # Phase 2: open the reader if a book_id was given.
        if book_id:
            reader_log: list[dict] = []
            await _record_request_responses(page, reader_log)
            reader_url = f"https://epub.yourcloudlibrary.com/read/{book_id}"
            print(f"\n=== Navigating to reader: {reader_url} ===", flush=True)
            try:
                await page.goto(reader_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                print(f"reader goto failed: {exc}", file=sys.stderr)

            # Give the SPA + reader time to bootstrap and request content.
            await asyncio.sleep(15)

            # Try a few page-turns so we capture content-fetch traffic.
            for _ in range(5):
                try:
                    await page.keyboard.press("ArrowRight")
                    await asyncio.sleep(2)
                except Exception:
                    break

            reader_state = await _snapshot_state(page, context, label="reader_loaded")
            (OUTPUT_DIR / "state-reader.json").write_text(
                json.dumps(reader_state, indent=2, default=str)
            )
            (OUTPUT_DIR / "network-reader.json").write_text(
                json.dumps(reader_log, indent=2)
            )
            try:
                await page.screenshot(path=str(OUTPUT_DIR / "reader.png"))
            except Exception as exc:
                print(f"screenshot failed: {exc}", file=sys.stderr)

            print(
                f"  → saved {len(reader_log)} reader-network entries to "
                f"network-reader.json",
                flush=True,
            )

        print(f"\nDONE. Capture written to {OUTPUT_DIR}/.", flush=True)
        print("Closing browser in 5s...", flush=True)
        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
