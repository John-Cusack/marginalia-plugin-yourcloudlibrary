"""Discovery → audit → enable → load → upgrade → disable, through core's own CLI.

Runs only where the plugin is installed as a distribution next to
``research-engine>=0.6`` (the release smoke venv or CI) and ``RE_DB_URL`` points at
a server for the disposable test database. Everything goes through the
``research-engine`` executable, so it exercises the contract operators use.

The import sentinel is a ``sitecustomize`` on the child's ``PYTHONPATH`` that
records any import of ``ycl``: discovery and audit must not execute plugin code.

The manifest-change step edits the *installed* ``ycl/plugin.yaml`` and restores it,
so it also requires ``YCL_LIFECYCLE_ALLOW_MANIFEST_EDIT=1``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._core_fixtures import TEST_DB_NAME, require_core
from tests.integration.conftest import fake_session_cookies

pytestmark = pytest.mark.integration

PLUGIN_ID = "yourcloudlibrary"
TOOL_IDS = [
    f"{PLUGIN_ID}.{name}"
    for name in ("auth_status", "search_catalog", "scrape_book", "ingest_book", "check_book",
                 "sync_loans", "list_books", "record_borrow", "forget_book", "acquire_and_ingest")
]


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    require_core()
    exe = shutil.which("research-engine")
    if exe is None:
        pytest.skip("research-engine executable not on PATH")
    points = importlib.metadata.entry_points(group="research_engine.plugins")
    if PLUGIN_ID not in points.names:
        pytest.skip("plugin distribution is not installed in this environment")
    from research_engine.testing import resolve_test_db_url

    root = tmp_path_factory.mktemp("lifecycle")
    sentinel_dir = root / "sentinel"
    sentinel_dir.mkdir()
    marker = root / "ycl-imported"
    (sentinel_dir / "sitecustomize.py").write_text(textwrap.dedent(f"""
        import importlib.abc, sys
        class _Watch(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "ycl" or name.startswith("ycl."):
                    open({str(marker)!r}, "a").write(name + "\\n")
                return None
        sys.meta_path.insert(0, _Watch())
    """))
    env = {
        **os.environ,
        "RE_DB_URL": resolve_test_db_url(os.environ.get("RE_DB_URL"), test_db=TEST_DB_NAME),
        "RE_DATA_DIR": str(root / "re-data"),
        "PYTHONPATH": str(sentinel_dir),
    }

    def run(*args: str, check: bool = True) -> str:
        proc = subprocess.run([exe, *args], env=env, capture_output=True, text=True, timeout=300)
        output = proc.stdout + proc.stderr
        if check:
            assert proc.returncode == 0, output
        return output

    run("db", "upgrade")
    return run, marker, Path(env["RE_DATA_DIR"])


def _installed_manifest() -> Path:
    dist = importlib.metadata.distribution("research-engine-plugin-yourcloudlibrary")
    return Path(dist.locate_file("ycl/plugin.yaml"))


def test_1_installed_wheel_is_available_without_import(cli):
    run, marker, _ = cli
    listing = run("plugin", "list")
    assert PLUGIN_ID in listing
    assert "available" in listing.lower()
    assert not marker.exists(), marker.read_text()


def test_2_audit_shows_permissions_and_every_contribution(cli):
    run, marker, _ = cli
    audit = run("plugin", "audit", PLUGIN_ID)
    for token in ["egress", "ebook.yourcloudlibrary.com", "epubservice.yourcloudlibrary.com",
                  "subprocess", "plugin_data", "ingest", "ycl_book", PLUGIN_ID, *TOOL_IDS]:
        assert token in audit, token
    assert hashlib.sha256(_installed_manifest().read_bytes()).hexdigest() in audit
    assert not marker.exists(), marker.read_text()


def test_3_enable_records_version_hash_and_permissions(cli):
    run, _, _ = cli
    run("plugin", "enable", PLUGIN_ID, "--yes")
    audit = run("plugin", "audit", PLUGIN_ID)
    assert "enabled" in audit.lower()
    assert "0.3.0" in audit
    assert hashlib.sha256(_installed_manifest().read_bytes()).hexdigest() in audit


def test_4_load_registers_all_contributions(cli):
    run, _, _ = cli
    doctor = run("plugin", "doctor", PLUGIN_ID)
    assert "error" not in doctor.lower(), doctor
    for token in [*TOOL_IDS, "ycl_book", PLUGIN_ID]:
        assert token in doctor or token.replace(".", "_") in doctor, token


def test_5_manifest_change_requires_approval(cli):
    if os.environ.get("YCL_LIFECYCLE_ALLOW_MANIFEST_EDIT") != "1":
        pytest.skip("set YCL_LIFECYCLE_ALLOW_MANIFEST_EDIT=1 to edit the installed manifest")
    run, _, _ = cli
    manifest = _installed_manifest()
    original = manifest.read_bytes()
    try:
        manifest.write_bytes(original + b"\n# lifecycle test: hash change\n")
        assert "pending" in run("plugin", "list").lower()
        run("plugin", "approve-upgrade", PLUGIN_ID, "--yes")
        assert "pending" not in run("plugin", "list").lower()
    finally:
        manifest.write_bytes(original)
    run("plugin", "approve-upgrade", PLUGIN_ID, "--yes", check=False)


def test_6_disable_leaves_plugin_data_untouched(cli):
    run, _, data_root = cli
    from ycl._paths import PluginPaths
    from ycl.session.cookies import CookieStore

    paths = PluginPaths(data_root / "plugin-data" / PLUGIN_ID)
    CookieStore(paths.cookie_path).save(fake_session_cookies())
    paths.borrows_path.write_text('{"FixtureLibrary": {}}')
    extract = paths.text_path_for("FixtureLibrary", "fixture0001")
    extract.parent.mkdir(parents=True, exist_ok=True)
    extract.write_text("fixture text")
    before = {p: p.read_bytes() for p in (paths.cookie_path, paths.borrows_path, extract)}

    run("plugin", "disable", PLUGIN_ID)

    assert {p: p.read_bytes() for p in before} == before
