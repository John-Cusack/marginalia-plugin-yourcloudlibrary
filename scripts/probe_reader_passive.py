"""Passive probe: open a persistent-profile Chromium, record everything you do.

Usage:
    uv run python scripts/probe_reader_passive.py [<BOOK_ID>]

Defaults BOOK_ID to onc5689 and uses the persistent profile at
scratch/auth-capture/profile/. The profile persists across runs, so once
you've logged in once, future runs come up already authenticated.

What you do:
    1. The browser opens to the book's detail page on YCL.
    2. If not authed (first run), log in via your library's normal flow.
    3. Click "Read" / "Begin reading" on the detail page.
    4. Read a few pages — turn at least 5–10 pages so we see content traffic.
    5. Close the browser window.

The script records every request + response body for yourcloudlibrary.com
hosts and dumps it to scratch/auth-capture/passive-{capture-id}.json plus a
final state snapshot, then exits.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

OUTPUT_DIR = Path("scratch/auth-capture")
PROFILE_DIR = OUTPUT_DIR / "profile"
SESSION_TIMEOUT_SECONDS = 1800  # 30 min hard cap; user closing browser ends sooner


def _is_yourcloudlibrary(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.endswith("yourcloudlibrary.com")


def _attach_recorders(page, log: list[dict]) -> None:
    def on_request(req):
        log.append(
            {
                "phase": "request",
                "ts": time.time(),
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
                "ts": time.time(),
                "method": req.method,
                "url": resp.url,
                "status": resp.status,
                "resource_type": req.resource_type,
                "headers": dict(resp.headers),
            }
            if _is_yourcloudlibrary(resp.url):
                ct = (resp.headers.get("content-type") or "").lower()
                rt = req.resource_type
                # Capture body for JSON / text / fetch / xhr / document.
                # Skip stylesheets and images — we don't care about asset bytes.
                if rt in ("xhr", "fetch", "document", "websocket") or "json" in ct or "xml" in ct:
                    try:
                        body = await resp.body()
                        if len(body) < 500_000:
                            try:
                                entry["body"] = body.decode("utf-8")
                            except UnicodeDecodeError:
                                entry["body_b64"] = __import__("base64").b64encode(body).decode("ascii")
                                entry["body_kind"] = "binary"
                        else:
                            entry["body_truncated_at"] = 500_000
                            entry["body"] = body[:500_000].decode("utf-8", errors="replace")
                    except Exception as exc:
                        entry["body_error"] = str(exc)
            log.append(entry)
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", lambda r: asyncio.create_task(on_response(r)))


async def _snapshot_state(page, context, *, label: str) -> dict:
    snapshot: dict = {
        "label": label,
        "url": page.url if not page.is_closed() else "<closed>",
    }
    try:
        snapshot["title"] = await page.title()
    except Exception as exc:
        snapshot["title_error"] = str(exc)
    try:
        snapshot["cookies"] = await context.cookies()
    except Exception as exc:
        snapshot["cookies_error"] = str(exc)
    for storage in ("localStorage", "sessionStorage"):
        try:
            snapshot[storage] = await page.evaluate(
                f"() => Object.fromEntries(Object.entries({storage}))"
            )
        except Exception as exc:
            snapshot[f"{storage}_error"] = str(exc)
    return snapshot


async def main() -> None:
    book_id = sys.argv[1] if len(sys.argv) > 1 else "onc5689"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    capture_id = time.strftime("%Y%m%d-%H%M%S")
    network_log: list[dict] = []
    detail_url = (
        f"https://ebook.yourcloudlibrary.com/library/PalmBeachCountyLibrarySystem"
        f"/detail/{book_id}"
    )

    async with async_playwright() as pw:
        # Persistent context — cookies and localStorage survive across runs.
        context = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR.resolve()),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        # First page that opens (or new one if none).
        page = context.pages[0] if context.pages else await context.new_page()
        _attach_recorders(page, network_log)
        # Also attach to any subsequently opened tabs.
        context.on(
            "page",
            lambda p: (_attach_recorders(p, network_log), None)[1],
        )

        print(f"\n=== Opening {detail_url} ===", flush=True)
        print(
            "If not already logged in, complete your library's login flow.\n"
            "Then click 'Read' / 'Begin reading' and turn 5+ pages.\n"
            "When done, close the browser window — the script will save and exit.\n",
            flush=True,
        )
        try:
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            print(f"goto failed: {exc}", file=sys.stderr)

        # Wait for the user to finish (browser close OR timeout).
        deadline = time.time() + SESSION_TIMEOUT_SECONDS
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                # If all pages are closed, the user closed the window — wrap up.
                if not context.pages:
                    print("All pages closed — capturing final state.", flush=True)
                    break
            except Exception:
                # Context disconnected (browser quit) — wrap up.
                print("Context disconnected — capturing final state.", flush=True)
                break

        # Try to grab a final snapshot. Pick the last live page if any.
        live_pages = [p for p in context.pages if not p.is_closed()]
        final_state: dict | None = None
        if live_pages:
            try:
                final_state = await _snapshot_state(
                    live_pages[-1], context, label="passive_final"
                )
            except Exception as exc:
                print(f"snapshot failed: {exc}", file=sys.stderr)

        out_state = OUTPUT_DIR / f"passive-state-{capture_id}.json"
        out_net = OUTPUT_DIR / f"passive-network-{capture_id}.json"
        out_state.write_text(
            json.dumps(final_state or {"label": "no_live_page"}, indent=2, default=str)
        )
        out_net.write_text(json.dumps(network_log, indent=2))
        print(
            f"\nDONE. {len(network_log)} network entries → {out_net}",
            flush=True,
        )
        print(f"      state → {out_state}", flush=True)

        try:
            await context.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
