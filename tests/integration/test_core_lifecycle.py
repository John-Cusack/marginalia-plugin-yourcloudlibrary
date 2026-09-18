"""Discovery → audit → enable → load → upgrade → disable, through core's own CLI.

Runs only where the plugin is installed as a distribution next to
``marginalia-ai>=0.6`` (the release smoke venv or CI) and ``RE_DB_URL`` points at
a server for the disposable test database. Everything goes through the
``research-engine`` executable, so it exercises the contract operators use.

The import sentinel is a ``sitecustomize`` on the child's ``PYTHONPATH`` that
records any import of ``ycl``: discovery and audit must not execute plugin code.

The manifest-change step edits the *installed* ``ycl/plugin.yaml`` and restores it,
so it also requires ``YCL_LIFECYCLE_ALLOW_MANIFEST_EDIT=1``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Cli:
    """The installed `research-engine` command, wired at a disposable database."""

    exe: str
    env: dict[str, str]
    import_marker: Path
    data_root: Path

    def run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            [self.exe, *args], env=self.env, capture_output=True, text=True, timeout=300
        )
        output = proc.stdout + proc.stderr
        if check:
            assert proc.returncode == 0, output
        return output


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    require_core()
    exe = shutil.which("research-engine")
    if exe is None:
        pytest.skip("research-engine executable not on PATH")
    points = importlib.metadata.entry_points(group="research_engine.plugins")
    if PLUGIN_ID not in points.names:
        pytest.skip("plugin distribution is not installed in this environment")
    from research_engine.testing import ensure_test_database, resolve_test_db_url

    db_url = resolve_test_db_url(os.environ.get("RE_DB_URL"), test_db=TEST_DB_NAME)
    if not asyncio.run(ensure_test_database(db_url)):
        pytest.skip("No reachable Postgres for the disposable test database (set RE_DB_URL)")

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
        "RE_DB_URL": db_url,
        "RE_DATA_DIR": str(root / "re-data"),
        "PYTHONPATH": str(sentinel_dir),
        # `serve` builds core's full container, and a base install has no local
        # embedding or reranking model. The `plugin` commands need neither.
        "RE_EMBEDDING_PROVIDER": "remote_api",
        "RE_INFERENCE_BASE_URL": os.environ.get("RE_INFERENCE_BASE_URL", "http://127.0.0.1:9"),
        "RE_RERANKER_PROVIDER": "none",
    }

    cli = Cli(exe=exe, env=env, import_marker=marker, data_root=Path(env["RE_DATA_DIR"]))
    # Activation state lives in the database and outlives a test run, so a
    # re-run would start from `disabled`. `forget` drops only that audit row.
    cli.run("plugin", "forget", PLUGIN_ID, "--yes", check=False)
    marker.unlink(missing_ok=True)
    return cli


def _installed_manifest() -> Path:
    dist = importlib.metadata.distribution("marginalia-ai-plugin-yourcloudlibrary")
    return Path(dist.locate_file("ycl/plugin.yaml"))


def test_1_installed_wheel_is_available_without_import(cli):
    listing = cli.run("plugin", "list")
    assert PLUGIN_ID in listing
    assert "available" in listing.lower()
    assert not cli.import_marker.exists(), cli.import_marker.read_text()


def test_2_audit_shows_permissions_and_every_contribution(cli):
    audit = cli.run("plugin", "audit", PLUGIN_ID)
    for token in ["egress", "ebook.yourcloudlibrary.com", "epubservice.yourcloudlibrary.com",
                  "subprocess", "plugin_data", "ingest", "ycl_book", PLUGIN_ID, *TOOL_IDS]:
        assert token in audit, token
    assert hashlib.sha256(_installed_manifest().read_bytes()).hexdigest() in audit
    assert not cli.import_marker.exists(), cli.import_marker.read_text()


def test_3_enable_records_version_hash_and_permissions(cli):
    cli.run("plugin", "enable", PLUGIN_ID, "--yes")
    audit = cli.run("plugin", "audit", PLUGIN_ID)
    assert "enabled" in audit.lower()
    assert "0.3.0" in audit
    assert hashlib.sha256(_installed_manifest().read_bytes()).hexdigest() in audit


async def _mcp_surface(exe: str, env: dict[str, str]) -> tuple[set[str], dict]:
    """Tool names the running server publishes, plus two live tool results.

    Goes through MCP because that is where "the plugin loaded" is observable:
    ``plugin doctor`` reports state only.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=exe, args=["serve"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        results = {}
        for name, arguments in (
            (f"{PLUGIN_ID}.auth_status", {}),
            ("search_sources", {"query": "augustine", "sources": [PLUGIN_ID], "limit": 1}),
        ):
            called = await session.call_tool(name, arguments)
            results[name] = json.loads(called.content[0].text)
        return names, results


def test_4_load_registers_all_tools_and_the_provider(cli):
    assert f"{PLUGIN_ID}: enabled" in cli.run("plugin", "doctor", PLUGIN_ID)

    names, results = asyncio.run(_mcp_surface(cli.exe, cli.env))

    # Core publishes plugin tools under their manifest ids.
    assert set(TOOL_IDS) <= names, sorted(names)
    # A plugin tool really runs, and reports the state of this data directory.
    status = results[f"{PLUGIN_ID}.auth_status"]
    assert status["authenticated"] is False
    assert "research-engine-ycl-login" in status["hint"]
    # The source-search provider is in the fan-out. A cold provider contributes
    # nothing on the first round (it warms in the background), which is fine here.
    assert results["search_sources"]["providers_queried"] == [PLUGIN_ID]


def test_5_manifest_change_requires_approval(cli):
    if os.environ.get("YCL_LIFECYCLE_ALLOW_MANIFEST_EDIT") != "1":
        pytest.skip("set YCL_LIFECYCLE_ALLOW_MANIFEST_EDIT=1 to edit the installed manifest")
    manifest = _installed_manifest()
    original = manifest.read_bytes()
    try:
        manifest.write_bytes(original + b"\n# lifecycle test: hash change\n")
        assert "pending" in cli.run("plugin", "list").lower()
        cli.run("plugin", "approve-upgrade", PLUGIN_ID, "--yes")
        assert "pending" not in cli.run("plugin", "list").lower()
    finally:
        manifest.write_bytes(original)
    cli.run("plugin", "approve-upgrade", PLUGIN_ID, "--yes", check=False)


def test_6_disable_leaves_plugin_data_untouched(cli):
    from ycl._paths import PluginPaths
    from ycl.session.cookies import CookieStore

    paths = PluginPaths(cli.data_root / "plugin-data" / PLUGIN_ID)
    CookieStore(paths.cookie_path).save(fake_session_cookies())
    paths.borrows_path.write_text('{"FixtureLibrary": {}}')
    extract = paths.text_path_for("FixtureLibrary", "fixture0001")
    extract.parent.mkdir(parents=True, exist_ok=True)
    extract.write_text("fixture text")
    before = {p: p.read_bytes() for p in (paths.cookie_path, paths.borrows_path, extract)}

    cli.run("plugin", "disable", PLUGIN_ID)

    assert {p: p.read_bytes() for p in before} == before
