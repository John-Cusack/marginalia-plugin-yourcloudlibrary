"""Click 'Login' on the Web Patron app, capture what kind of auth surface appears."""

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
        await page.wait_for_timeout(3000)

        # Find and click the Login button.
        login_btn = page.get_by_role("button", name="Login").first
        try:
            await login_btn.click(timeout=5000)
        except Exception as exc:
            out["click_error"] = str(exc)

        await page.wait_for_timeout(3000)

        out["url_after_click"] = page.url
        out["title_after_click"] = await page.title()
        out["body_text"] = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 3000)"
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
        out["password_inputs"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('input[type=\"password\"]')).map(i => ({"
            "  name: i.name, id: i.id, placeholder: i.placeholder, "
            "  parent_form: i.form ? i.form.id : null"
            "}))"
        )
        out["modal_visible"] = await page.evaluate(
            "() => !!document.querySelector('[role=\"dialog\"], .modal, .login-modal, [class*=\"login\" i]')"
        )

        await page.screenshot(path="scratch/login-click.png", full_page=False)
        await browser.close()

    Path("scratch").mkdir(exist_ok=True)
    Path("scratch/probe-login-click.json").write_text(json.dumps(out, indent=2))
    print("url_after_click:", out["url_after_click"])
    print("title_after_click:", out["title_after_click"])
    print("modal_visible:", out["modal_visible"])
    print(f"forms: {len(out['forms'])}")
    for f in out["forms"]:
        print(f"  action={f['action']!r}  inputs={[(i['name'], i['type']) for i in f['inputs']]}")
    print(f"password_inputs: {out['password_inputs']}")
    print()
    print("=== body[:2500] ===")
    print(out["body_text"][:2500])


if __name__ == "__main__":
    asyncio.run(main())
