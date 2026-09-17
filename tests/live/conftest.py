"""Live tier: the real YourCloudLibrary site, with your existing authorized session.

Opt-in only: ``pytest -m live``. It reads the session saved by
``research-engine-ycl-login`` in the default plugin data directory and copies it
into a temp data directory, so borrow records and extracts written by the tests
never land in your real plugin data. Tests that borrow a real book additionally
need ``YCL_LIVE_ACQUIRE=1`` and the integration tier's disposable database.
"""

from __future__ import annotations

import shutil
import time

import pytest

from tests._core_fixtures import ingestion  # noqa: F401 — re-exported fixture


@pytest.fixture
def live_context(plugin_context, plugin_paths):
    """A temp-dir PluginContext holding a copy of the saved, unexpired session."""
    from ycl._paths import resolve_paths
    from ycl.api.cookies import cookie_expiry
    from ycl.api.errors import LOGIN_COMMAND
    from ycl.session.cookies import CookieStore

    saved = resolve_paths(None).cookie_path
    cookies = CookieStore(saved).load()
    if not cookies:
        pytest.skip(f"No saved YCL session at {saved} — run `{LOGIN_COMMAND}`.")
    expires = cookie_expiry(cookies, "__config_PROD")
    if expires is not None and expires < time.time():
        pytest.skip(f"Saved YCL catalog session expired — re-run `{LOGIN_COMMAND}`.")
    plugin_paths.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(saved, plugin_paths.cookie_path)
    return plugin_context


@pytest.fixture
def can_read(live_context, plugin_paths):
    """Skip unless the session can fetch book content (reading cookie unexpired)."""
    from ycl.api.cookies import reading_session_status
    from ycl.session.cookies import CookieStore

    status = reading_session_status(CookieStore(plugin_paths.cookie_path).load())
    if not status["ok"]:
        pytest.skip(f"YCL reading session unavailable ({status['reason']}) — re-run login.")
    return live_context
