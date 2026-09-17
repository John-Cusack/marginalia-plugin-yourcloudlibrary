"""One data directory for every consumer, and session files that stay private."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ycl import _paths
from ycl._paths import PluginPaths, default_data_dir, resolve_paths
from ycl.api.cookies import redact_secrets
from ycl.session.cookies import CookieStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(_paths.CORE_DATA_DIR_ENV, raising=False)


def test_default_matches_core_plugin_data_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_data_dir() == tmp_path / ".research-engine" / "plugin-data" / "yourcloudlibrary"


def test_core_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "re"))
    assert default_data_dir() == tmp_path / "re" / "plugin-data" / "yourcloudlibrary"


def test_context_data_dir_wins_over_defaults(monkeypatch, tmp_path, plugin_context):
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "re"))
    paths = resolve_paths(plugin_context)
    assert paths.data_dir == plugin_context.data_dir
    assert paths.cookie_path == plugin_context.data_dir / "cookies.json"
    assert paths.borrows_path == plugin_context.data_dir / "borrows.json"
    assert paths.text_path_for("Lib", "b1") == plugin_context.data_dir / "extracted/Lib/b1.txt"
    assert paths.chapters_path_for("Lib", "b1").name == "b1.chapters.json"
    assert paths.partial_path_for("Lib", "b1").name == "b1.partial.txt"


def test_no_context_uses_default(monkeypatch, tmp_path):
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path))
    assert resolve_paths(None) == PluginPaths(tmp_path / "plugin-data" / "yourcloudlibrary")


def test_nothing_resolves_inside_the_installed_package(plugin_paths):
    package_dir = Path(_paths.__file__).resolve().parent
    assert package_dir not in resolve_paths(None).data_dir.resolve().parents


def test_cookie_store_writes_owner_only(tmp_path):
    path = tmp_path / "d" / "cookies.json"
    CookieStore(path).save([{"name": "__session_PROD", "value": "secret"}])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert CookieStore(path).load() == [{"name": "__session_PROD", "value": "secret"}]
    # Overwrite keeps the mode and leaves no temp files behind.
    CookieStore(path).save([])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["cookies.json"]


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("token eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3MDB9.c2lnbmF0dXJl here", "eyJhbGci"),
        ("redirect https://sso.example.org/cb?code=abc123DEF&x=1", "abc123DEF"),
        ("opaque " + "Qk" * 30 + " end", "Qk" * 30),
    ],
)
def test_redact_secrets_masks_tokens(text, leaked):
    assert leaked not in redact_secrets(text)


def test_redact_secrets_masks_known_cookie_values_and_keeps_paths():
    cookies = [{"name": "__session_PROD", "value": "short-secret-1"}]
    out = redact_secrets(
        "failed at /home/u/.research-engine/plugin-data/yourcloudlibrary/cookies.json "
        "with short-secret-1",
        cookies,
    )
    assert "short-secret-1" not in out
    assert "plugin-data/yourcloudlibrary/cookies.json" in out
