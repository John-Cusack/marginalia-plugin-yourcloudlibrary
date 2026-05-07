"""One-time interactive login for the YourCloudLibrary plugin.

Run this once when first installing the plugin (and again whenever the
session expires — typically every ~30 days):

    uv run python -m ycl.cli.login

A Chromium window opens. Sign in to your library however you normally
do (the YCL marketing site has a "Find your library" widget that works
for most). The script polls every few seconds for the YCL session
cookies; when they appear, it captures them to disk and exits.

After this completes, all the MCP tools work via plain httpx — no
browser, no UI dependencies.
"""

from __future__ import annotations

import asyncio
import sys
import time

import structlog
from playwright.async_api import async_playwright

from .._paths import COOKIE_PATH
from ..api.cookies import decode_config_cookie
from ..session.cookies import CookieStore

log = structlog.get_logger(__name__)

# Where the user lands first. The YCL marketing site has a "Find your
# library" widget that handles all the per-library auth flow variations.
START_URL = "https://www.yourcloudlibrary.com/"

# How long the user has to complete login before we give up.
LOGIN_TIMEOUT_SECONDS = 900  # 15 min — generous for SSO redirects, MFA, etc.

# Cookies we need on the YCL session. ``__config_PROD`` carries the
# library identity; ``__session_PROD`` is the actual session JWT.
_REQUIRED_COOKIES = ("__config_PROD", "__session_PROD")


async def _has_required_cookies(context) -> bool:
    cookies = await context.cookies()
    names = {c["name"] for c in cookies}
    return all(req in names for req in _REQUIRED_COOKIES)


async def main() -> int:
    print(f"Opening {START_URL} — please complete your library's login flow.")
    print(
        "I'll poll every 3s for your YCL session cookies. The window will\n"
        "close automatically once they appear.\n"
    )
    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    async with async_playwright() as pw:
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
        try:
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            print(f"goto {START_URL} failed: {exc}", file=sys.stderr)

        captured = False
        while time.time() < deadline:
            try:
                if await _has_required_cookies(context):
                    captured = True
                    break
            except Exception:
                # Browser might have been closed by the user; treat as failure.
                break
            await asyncio.sleep(3)

        if not captured:
            print(
                "Timed out (or browser closed) before required cookies appeared.",
                file=sys.stderr,
            )
            try:
                await browser.close()
            except Exception:
                pass
            return 1

        cookies = await context.cookies()
        try:
            library = decode_config_cookie(cookies)
            print(f"Authenticated as patron at: {library.name} ({library.url_name})")
        except Exception as exc:
            print(f"warning: could not decode library info from cookie: {exc}")

        store = CookieStore(COOKIE_PATH)
        store.save(cookies)
        print(f"Saved {len(cookies)} cookies → {COOKIE_PATH}")
        try:
            await browser.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
