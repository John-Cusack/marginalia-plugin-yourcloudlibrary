"""Probe the working Web Patron app at /library/nypl/featured to learn the
auth + reader flow."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    out: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        await page.goto(
            "https://ebook.yourcloudlibrary.com/library/nypl/featured",
            wait_until="networkidle",
            timeout=30000,
        )
        await page.wait_for_timeout(4000)

        out["url"] = page.url
        out["title"] = await page.title()
        out["body_text_head"] = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 3000)"
        )

        # Login-related elements: any link/button with the word login/signin?
        out["login_elements"] = await page.evaluate(
            """
            () => {
                const out = [];
                const all = document.querySelectorAll('a, button');
                for (const el of all) {
                    const text = (el.textContent || '').trim();
                    const href = el.getAttribute('href') || null;
                    const id = el.id || null;
                    const cls = (el.className && el.className.toString) ? el.className.toString().slice(0, 80) : null;
                    if (/log\\s*in|sign\\s*in|account|my books|loans/i.test(text)) {
                        out.push({tag: el.tagName, text: text.slice(0, 80), href, id, cls});
                        if (out.length >= 30) break;
                    }
                }
                return out;
            }
            """
        )

        out["forms"] = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action, method: f.method,
                id: f.id, classes: f.className,
                inputs: Array.from(f.querySelectorAll('input')).map(i => ({
                    name: i.name, type: i.type, placeholder: i.placeholder, id: i.id
                }))
            }))
            """
        )

        # Iframes (the reader is likely inside one).
        out["iframes"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('iframe')).map(f => ({"
            "  id: f.id, name: f.name, src: f.src, "
            "  width: f.clientWidth, height: f.clientHeight"
            "}))"
        )

        await page.screenshot(path="scratch/app-nypl.png", full_page=False)
        await browser.close()

    Path("scratch").mkdir(exist_ok=True)
    Path("scratch/probe-app-nypl.json").write_text(json.dumps(out, indent=2))
    print("=== url ===", out["url"])
    print("=== title ===", out["title"])
    print("=== body[:2000] ===")
    print(out["body_text_head"][:2000])
    print()
    print(f"=== login_elements ({len(out['login_elements'])}) ===")
    for el in out["login_elements"][:15]:
        print(f"  <{el['tag']}> {el['text']!r}  href={el.get('href')!r}  id={el.get('id')!r}")
    print()
    print(f"=== forms ({len(out['forms'])}) ===")
    for f in out["forms"]:
        print(f"  action={f['action']!r}  inputs={[(i['name'], i['type']) for i in f['inputs']]}")
    print()
    print(f"=== iframes ({len(out['iframes'])}) ===")
    for f in out["iframes"]:
        print(f"  {f}")


if __name__ == "__main__":
    asyncio.run(main())
