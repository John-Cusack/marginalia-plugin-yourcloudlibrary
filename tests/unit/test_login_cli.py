"""research-engine-ycl-login without a browser: a fake ``playwright.async_api``.

Covers the operator-facing contract — exit codes, exact install commands for a
missing Playwright or Chromium, the 15-minute poll ending in a clean close,
validation before saving, owner-only storage under the data dir, and no cookie
values in output.
"""

from __future__ import annotations

import asyncio
import base64
import json
import stat
import sys
import types

import pytest

import ycl.cli.login as cli
from ycl.session.cookies import CookieStore

_CONFIG = {
    "library_info": {"name": "Palm Beach County Library System",
                     "urlName": "PalmBeachCountyLibrarySystem"},
    "library_config": {"reaktor_patron_id": 1},
    "login_info": {"library": "uuid", "barcode": "D0000001234", "state": "FL"},
}
SESSION_VALUE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwYXRyb24ifQ.c2VjcmV0LXNpZ25hdHVyZQ"


def _config_cookie(payload=_CONFIG) -> dict:
    value = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"name": "__config_PROD", "value": value, "domain": ".yourcloudlibrary.com"}


def _session_cookie() -> dict:
    return {"name": "__session_PROD", "value": SESSION_VALUE, "domain": ".yourcloudlibrary.com",
            "expires": -1}


class _FakeBrowser:
    def __init__(self, cookies):
        self.cookies = cookies
        self.closed = False
        self.visited: list[str] = []

    async def new_context(self, **_kw):
        browser = self

        class _Context:
            async def cookies(self):
                return browser.cookies

            async def new_page(self):
                class _Page:
                    async def goto(self, url, **_kw):
                        browser.visited.append(url)

                return _Page()

        return _Context()

    async def close(self):
        self.closed = True


def _install_playwright(monkeypatch, *, browser=None, launch_error=None):
    launches = []

    class _Chromium:
        async def launch(self, **kwargs):
            launches.append(kwargs)
            if launch_error is not None:
                raise launch_error
            return browser

    class _Manager:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=_Chromium())

        async def __aexit__(self, *exc):
            return False

    module = types.ModuleType("playwright.async_api")
    module.async_playwright = _Manager
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr(cli, "POLL_SECONDS", 0)
    return launches


def test_missing_playwright_prints_install_command(monkeypatch, plugin_paths, capsys):
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    assert asyncio.run(cli.login(plugin_paths)) == 2
    assert 'python -m pip install "playwright>=1.40"' in capsys.readouterr().err


def test_missing_chromium_prints_playwright_install_command(monkeypatch, plugin_paths, capsys):
    _install_playwright(
        monkeypatch,
        launch_error=Exception(
            "BrowserType.launch: Executable doesn't exist at /x/chrome\n"
            "Please run the following command to download new browsers: playwright install"
        ),
    )
    assert asyncio.run(cli.login(plugin_paths)) == 2
    assert "python -m playwright install chromium" in capsys.readouterr().err


def test_success_validates_saves_owner_only_and_hides_values(monkeypatch, plugin_paths, capsys):
    browser = _FakeBrowser([_config_cookie(), _session_cookie()])
    launches = _install_playwright(monkeypatch, browser=browser)

    assert asyncio.run(cli.login(plugin_paths)) == 0

    assert launches[0]["headless"] is False  # visible, interactive login
    assert browser.closed
    saved = CookieStore(plugin_paths.cookie_path).load()
    assert {c["name"] for c in saved} == {"__config_PROD", "__session_PROD"}
    assert stat.S_IMODE(plugin_paths.cookie_path.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert "Palm Beach County Library System" in out.out
    for secret in (SESSION_VALUE, _config_cookie()["value"], "D0000001234"):
        assert secret not in out.out + out.err


def test_timeout_saves_nothing_and_closes_browser(monkeypatch, plugin_paths, capsys):
    browser = _FakeBrowser([_config_cookie()])  # never gets __session_PROD
    _install_playwright(monkeypatch, browser=browser)

    assert asyncio.run(cli.login(plugin_paths, timeout_seconds=0)) == 1

    assert browser.closed
    assert not plugin_paths.cookie_path.exists()
    assert "Nothing was saved" in capsys.readouterr().err


def test_undecodable_session_is_not_saved(monkeypatch, plugin_paths):
    bad_config = {"name": "__config_PROD", "value": base64.b64encode(b"no json").decode()}
    browser = _FakeBrowser([bad_config, _session_cookie()])
    _install_playwright(monkeypatch, browser=browser)

    assert asyncio.run(cli.login(plugin_paths)) == 1
    assert not plugin_paths.cookie_path.exists()
    assert browser.closed


def test_login_timeout_is_fifteen_minutes():
    assert cli.LOGIN_TIMEOUT_SECONDS == 15 * 60


def test_start_url_prefers_named_then_saved_library(plugin_paths):
    assert cli.start_url(plugin_paths, None) == cli.DEFAULT_START_URL
    assert cli.start_url(plugin_paths, "SomeLibrary").endswith("/library/SomeLibrary/featured")
    CookieStore(plugin_paths.cookie_path).save([_config_cookie()])
    assert cli.start_url(plugin_paths, None).endswith(
        "/library/PalmBeachCountyLibrarySystem/featured"
    )


def test_run_uses_data_dir_flag(monkeypatch, tmp_path):
    seen = {}

    async def fake_login(paths, *, library=None):
        seen["paths"], seen["library"] = paths, library
        return 0

    monkeypatch.setattr(cli, "login", fake_login)
    assert cli.run(["--data-dir", str(tmp_path), "--library", "Lib_1"]) == 0
    assert seen["paths"].cookie_path == tmp_path / "cookies.json"
    assert seen["library"] == "Lib_1"


def test_run_rejects_non_urlname_library():
    with pytest.raises(SystemExit) as exc:
        cli.run(["--library", "evil.example/../x"])
    assert exc.value.code == 2


def test_main_is_a_zero_argument_sync_wrapper(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["research-engine-ycl-login", "migrate", "--from", "/nonexistent-ycl"])
    assert cli.main() == 0  # nothing to migrate
