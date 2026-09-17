"""``research-engine-ycl-login`` — capture a YourCloudLibrary session.

Run it once after installing the plugin, and again whenever reading stops
working (the reading cookie lasts about a day)::

    research-engine-ycl-login [--library URL_NAME] [--data-dir DIR]
    research-engine-ycl-login migrate [--help]

A visible Chromium window opens. Sign in to your library however you normally
do. The command polls every few seconds for the YCL session cookies; when they
appear it validates them, saves them (0600) under the plugin data directory, and
closes the browser. Nothing is read from stdin and no password is stored.

``migrate`` copies data from the pre-0.3.0 ``~/.marginalia`` location; see
:mod:`ycl.cli.migrate`.

The browser is not downloaded on install. If Chromium is missing this command
prints the exact ``playwright install`` command to run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import sys
import time
from pathlib import Path

import structlog

from .._paths import PluginPaths, default_data_dir
from ..api.cookies import decode_config_cookie, reading_session_status, redact_secrets
from ..api.errors import (
    LOGIN_COMMAND,
    PLAYWRIGHT_INSTALL_COMMAND,
    NotAuthenticatedError,
    browser_launch_error,
)
from ..session.cookies import CookieStore

# Where the user lands when we don't know their library. The marketing site's
# "Find your library" widget handles every per-library auth flow.
DEFAULT_START_URL = "https://www.yourcloudlibrary.com/"
# Known library: start on its catalog. Measured more reliable than the marketing
# page at producing both session cookies (see IMPL_NOTES.md).
LIBRARY_START_URL = "https://ebook.yourcloudlibrary.com/library/{url_name}/featured"

# How long the user has to complete login before we give up.
LOGIN_TIMEOUT_SECONDS = 900  # 15 min — generous for SSO redirects, MFA, etc.
POLL_SECONDS = 3

# ``__config_PROD`` carries the library identity; ``__session_PROD`` is the
# session the content endpoints require.
_REQUIRED_COOKIES = ("__config_PROD", "__session_PROD")

_URL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def main() -> int:
    """Console-script entry point: parse ``sys.argv`` and return an exit code."""
    return run(sys.argv[1:])


def run(argv: list[str]) -> int:
    # The library modules log at debug; a terminal command should only show
    # its own output and real problems.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    if argv and argv[0] == "migrate":
        from .migrate import run as run_migrate

        return run_migrate(argv[1:])

    parser = argparse.ArgumentParser(
        prog=LOGIN_COMMAND,
        description="Open a browser, sign in to YourCloudLibrary, and save the session.",
        epilog=f"Move pre-0.3.0 data with: {LOGIN_COMMAND} migrate --help",
    )
    parser.add_argument(
        "--library",
        metavar="URL_NAME",
        help="Library urlName (e.g. PalmBeachCountyLibrarySystem) to start on its catalog.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Plugin data directory (default: the one core passes to the plugin).",
    )
    args = parser.parse_args(argv)
    if args.library and not _URL_NAME.match(args.library):
        parser.error("--library must be a urlName: letters, digits, '-' or '_'.")
    paths = PluginPaths(args.data_dir.expanduser() if args.data_dir else default_data_dir())
    return asyncio.run(login(paths, library=args.library))


def start_url(paths: PluginPaths, library: str | None) -> str:
    """Catalog page for the named or previously saved library, else the marketing site."""
    if library is None:
        cookies = CookieStore(paths.cookie_path).load()
        with contextlib.suppress(NotAuthenticatedError, ValueError):
            saved = decode_config_cookie(cookies).url_name if cookies else ""
            library = saved if saved and _URL_NAME.match(saved) else None
    return LIBRARY_START_URL.format(url_name=library) if library else DEFAULT_START_URL


async def _has_required_cookies(context) -> bool:
    names = {c["name"] for c in await context.cookies()}
    return all(req in names for req in _REQUIRED_COOKIES)


async def login(
    paths: PluginPaths,
    *,
    library: str | None = None,
    timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "Playwright is not installed in this environment. Install it with:\n"
            f"  {PLAYWRIGHT_INSTALL_COMMAND}",
            file=sys.stderr,
        )
        return 2

    url = start_url(paths, library)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
        except Exception as exc:
            missing = browser_launch_error(exc)
            if missing is not None:
                print(f"{missing} Install it with:\n  {missing.hint}", file=sys.stderr)
                return 2
            print(f"Could not start Chromium: {redact_secrets(str(exc))}", file=sys.stderr)
            return 1
        try:
            return await _capture(browser, paths, url, timeout_seconds)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


async def _capture(browser, paths: PluginPaths, url: str, timeout_seconds: float) -> int:
    print(f"Opening {url} — please complete your library's login flow.")
    print(
        f"Polling every {POLL_SECONDS}s for your YCL session cookies. The window closes\n"
        "automatically once they appear.\n"
    )
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        # Not fatal: the user can still navigate in the open window.
        print(f"Could not load {url}: {redact_secrets(str(exc))}", file=sys.stderr)

    deadline = time.monotonic() + timeout_seconds
    captured = False
    while time.monotonic() < deadline:
        try:
            if await _has_required_cookies(context):
                captured = True
                break
        except Exception:
            # The user closed the browser; treat as failure.
            break
        await asyncio.sleep(POLL_SECONDS)

    if not captured:
        print(
            "Timed out (or the browser was closed) before the session cookies appeared. "
            "Nothing was saved.",
            file=sys.stderr,
        )
        return 1

    cookies = await context.cookies()
    try:
        library = decode_config_cookie(cookies)
    except (NotAuthenticatedError, ValueError) as exc:
        # Saving an undecodable session would replace a working one with junk.
        print(
            f"The library session could not be read ({redact_secrets(str(exc))}). "
            "Nothing was saved; try again.",
            file=sys.stderr,
        )
        return 1
    print(f"Authenticated as a patron of: {library.name} ({library.url_name})")

    # __session_PROD (the reading/scrape cookie) is short-lived (~1 day) and
    # epubservice enforces it strictly. Tell the user their scrape window so a
    # later 401 isn't a mystery.
    reading = reading_session_status(cookies)
    if reading["ok"]:
        exp = reading.get("expires")
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)) if exp else "this session"
        print(
            f"Reading/scrape enabled until ~{when}. The reading session is short-lived — "
            f"re-run {LOGIN_COMMAND} when ingest starts returning auth errors."
        )
    else:
        print(f"warning: {reading['detail']}")

    CookieStore(paths.cookie_path).save(cookies)
    print(f"Saved {len(cookies)} cookies → {paths.cookie_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
