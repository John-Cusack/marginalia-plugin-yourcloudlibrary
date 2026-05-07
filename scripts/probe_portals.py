"""Probe likely library-portal URL patterns and report what each returns."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

CANDIDATES = [
    "https://onc.yourcloudlibrary.com/",
    "https://onc.yourcloudlibrary.com/library/onc",
    "https://www.yourcloudlibrary.com/library/onc",
    "https://www.yourcloudlibrary.com/onc",
    "https://ebook.yourcloudlibrary.com/library/onc/featured",
    "https://ebook.yourcloudlibrary.com/library/onc",
    "https://ebook.yourcloudlibrary.com/uisvc/onc/Web/",
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
        await page.wait_for_timeout(3000)
        out["final_url"] = page.url
        out["title"] = await page.title()
        out["body_text_head"] = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 600)"
        )
        out["has_login_form"] = bool(
            await page.query_selector(
                'form[action*="login" i], input[type="password"], '
                'input[name*="card" i], input[name*="barcode" i], '
                'input[name*="pin" i]'
            )
        )
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
        Path("scratch/probe-portals.json").write_text(json.dumps(results, indent=2))
        for r in results:
            print()
            print(f"=== {r['url']} ===")
            print(f"  status: {r.get('status')}  final_url: {r.get('final_url')}")
            print(f"  title: {r.get('title')!r}")
            print(f"  has_login_form: {r.get('has_login_form')}")
            print(f"  body[:300]: {r.get('body_text_head', '')[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
