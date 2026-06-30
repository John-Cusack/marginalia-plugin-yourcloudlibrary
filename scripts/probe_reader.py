"""One-off probe — load the YCL reader URL and dump DOM structure, no auth.

Run with:
    uv run python scripts/probe_reader.py onc5689

Captures: final URL after redirects, page title, all iframes, the first 200
elements with class/id attributes, any visible expired-loan text, network
requests to yourcloudlibrary domains. Output is written to scratch/probe-{book_id}.json
so it can be reviewed offline.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def probe(book_id: str, *, headless: bool = True) -> dict:
    url = f"https://epub.yourcloudlibrary.com/read/{book_id}"
    out: dict = {"input_url": url, "book_id": book_id}
    network: list[dict] = []
    console: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        page.on("console", lambda m: console.append({"type": m.type, "text": m.text[:300]}))
        page.on(
            "request",
            lambda r: network.append({"method": r.method, "url": r.url}),
        )

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            out["initial_status"] = response.status if response else None
        except Exception as exc:
            out["goto_error"] = str(exc)

        # Give SPA a moment to bootstrap.
        await page.wait_for_timeout(8000)

        out["final_url"] = page.url
        out["title"] = await page.title()

        # Top-level body text (first 2000 chars) — catches expired banners,
        # error messages, login prompts.
        out["body_text_head"] = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 2000)"
        )

        # All iframes on the page.
        out["iframes"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('iframe')).map(f => ({"
            "  id: f.id, name: f.name, src: f.src, "
            "  width: f.clientWidth, height: f.clientHeight"
            "}))"
        )

        # Salient elements: anything with id or interesting class names.
        out["salient_elements"] = await page.evaluate(
            """
            () => {
                const seen = new Set();
                const out = [];
                const nodes = document.querySelectorAll(
                    '[id], [class*="reader" i], [class*="book" i], [class*="page" i], '
                    + '[class*="content" i], [class*="expired" i], [class*="error" i], '
                    + '[data-testid], [role="main"], [aria-label]'
                );
                for (const n of nodes) {
                    const key = (n.tagName + '#' + (n.id || '') + '.' + (n.className || ''));
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({
                        tag: n.tagName,
                        id: n.id || null,
                        class: (n.className && n.className.toString) ? n.className.toString().slice(0, 200) : null,
                        testid: n.getAttribute('data-testid'),
                        aria_label: n.getAttribute('aria-label'),
                        text_head: (n.textContent || '').trim().slice(0, 120),
                    });
                    if (out.length >= 200) break;
                }
                return out;
            }
            """
        )

        # If iframes exist, peek inside the first few.
        iframe_dumps = []
        for f in page.frames:
            if f == page.main_frame:
                continue
            try:
                iframe_dumps.append(
                    {
                        "url": f.url,
                        "name": f.name,
                        "title": await f.title(),
                        "body_text_head": await f.evaluate(
                            "() => (document.body && document.body.innerText || '').slice(0, 1000)"
                        ),
                        "salient_ids": await f.evaluate(
                            "() => Array.from(document.querySelectorAll('[id]')).map(n => n.id).slice(0, 50)"
                        ),
                    }
                )
            except Exception as exc:
                iframe_dumps.append({"url": f.url, "error": str(exc)})
        out["frames"] = iframe_dumps

        # Take a screenshot for visual inspection.
        scratch = Path("scratch")
        scratch.mkdir(exist_ok=True)
        screenshot_path = scratch / f"probe-{book_id}.png"
        await page.screenshot(path=str(screenshot_path), full_page=False)
        out["screenshot"] = str(screenshot_path)

        await browser.close()

    out["network_yourcloudlibrary"] = [
        n for n in network if "yourcloudlibrary" in n["url"]
    ][:50]
    out["console_messages_head"] = console[:30]
    return out


async def _main() -> None:
    book_id = sys.argv[1] if len(sys.argv) > 1 else "onc5689"
    headless = "--headed" not in sys.argv
    result = await probe(book_id, headless=headless)
    Path("scratch").mkdir(exist_ok=True)
    out_path = Path(f"scratch/probe-{book_id}.json")
    out_path.write_text(json.dumps(result, indent=2))
    print(f"wrote {out_path}  (final_url={result.get('final_url')!r}, title={result.get('title')!r})")
    print(f"body_text_head: {result.get('body_text_head', '')[:400]}")


if __name__ == "__main__":
    asyncio.run(_main())
