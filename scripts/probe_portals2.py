"""Round 2 — narrowing in on the real per-library portal URL pattern."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

CANDIDATES = [
    "https://ebook.yourcloudlibrary.com/uisvc/onc/Web/Default",
    "https://ebook.yourcloudlibrary.com/uisvc/onc/Web/Default.aspx",
    "https://ebook.yourcloudlibrary.com/uisvc/onc/Web/Library",
    "https://ebook.yourcloudlibrary.com/uisvc/onc/Web/BookDetail/onc5689",
    "https://ebook.yourcloudlibrary.com/library/details/onc5689",
    "https://ebook.yourcloudlibrary.com/library/onc/details/onc5689",
    "https://ebook.yourcloudlibrary.com/library/onc/featured",
    # If the prefix in book_id is NOT the library code, try a few real
    # libraries that use cloudLibrary widely.
    "https://ebook.yourcloudlibrary.com/library/lapl/featured",
    "https://ebook.yourcloudlibrary.com/library/nypl/featured",
    "https://ebook.yourcloudlibrary.com/library/sfpl/featured",
]


async def probe_url(pw, url: str) -> dict:
    out: dict = {"url": url}
    browser = await pw.chromium.launch(headless=True)
    try:
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            out["status"] = response.status if response else None
        except Exception as exc:
            out["goto_error"] = str(exc)
            return out
        await page.wait_for_timeout(2500)
        out["final_url"] = page.url
        out["title"] = await page.title()
        out["body_text_head"] = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 400)"
        )
        out["redirected_to_marketing"] = "yourcloudlibrary.com/en/home" in page.url
    finally:
        await browser.close()
    return out


async def main() -> None:
    async with async_playwright() as pw:
        results = []
        for url in CANDIDATES:
            print(f"probing {url} ...", flush=True)
            res = await probe_url(pw, url)
            results.append(res)
        Path("scratch").mkdir(exist_ok=True)
        Path("scratch/probe-portals2.json").write_text(json.dumps(results, indent=2))
        for r in results:
            redir = " → MARKETING" if r.get("redirected_to_marketing") else ""
            print(
                f"{r.get('status')} {r['url']}{redir}\n"
                f"    title={r.get('title')!r}"
            )


if __name__ == "__main__":
    asyncio.run(main())
