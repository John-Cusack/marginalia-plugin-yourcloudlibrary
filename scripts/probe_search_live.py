"""Headed/authenticated capture of the catalog-search XHR.

Loads the saved YCL cookies into a Playwright context, navigates to the search
results page, and records every yourcloudlibrary.com request so we can see which
endpoint returns the actual book hits (the Remix loader only returns facets).

Run: uv run python -m scripts.probe_search_live
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from ycl._paths import COOKIE_PATH
from ycl.api.cookies import decode_config_cookie
from ycl.session.cookies import CookieStore

QUERY = "harry potter"


def _to_pw_cookies(raw: list[dict]) -> list[dict]:
    out = []
    for c in raw:
        if "yourcloudlibrary.com" not in (c.get("domain") or ""):
            continue
        ck = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
        }
        out.append(ck)
    return out


async def main() -> None:
    raw = CookieStore(COOKIE_PATH).load()
    lib = decode_config_cookie(raw)
    slug = lib.url_name
    log: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(_to_pw_cookies(raw))

        page = await context.new_page()

        async def on_response(resp):
            url = resp.url
            host = urlparse(url).hostname or ""
            if not host.endswith("yourcloudlibrary.com"):
                return
            if resp.request.resource_type in ("image", "font", "stylesheet", "media"):
                return
            entry = {"url": url, "status": resp.status, "method": resp.request.method,
                     "rtype": resp.request.resource_type}
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    body = await resp.json()
                    entry["json_keys"] = list(body)[:20] if isinstance(body, dict) else f"list[{len(body)}]"
                    entry["body"] = body
                except Exception:
                    pass
            log.append(entry)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        url = f"https://ebook.yourcloudlibrary.com/library/{slug}/search?query={QUERY.replace(' ', '%20')}"
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(4000)
        print("final url:", page.url)

        Path("scratch").mkdir(exist_ok=True)
        Path("scratch/probe-search-live.json").write_text(json.dumps(log, indent=2, default=str))

        # Print the interesting (JSON) responses, biggest first.
        json_resps = [e for e in log if "json_keys" in e]
        json_resps.sort(key=lambda e: len(json.dumps(e.get("body", ""))), reverse=True)
        for e in json_resps:
            print(f"\n[{e['status']}] {e['method']} {e['url']}")
            print(f"    keys={e['json_keys']}")
            body = e.get("body")
            snippet = json.dumps(body, indent=2)[:1500]
            print("\n".join("    " + ln for ln in snippet.splitlines()[:40]))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
