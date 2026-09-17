"""Build the wheel and sdist and inspect what would actually be published.

Needs ``uv`` on PATH (skips otherwise). Asserts the discovery metadata core reads,
that every runtime resource ships, and that no session, borrowed text, capture,
or test stand-in can reach PyPI.
"""

from __future__ import annotations

import email.parser
import fnmatch
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = [
    "*.env", ".env*", "*cookies.json", "*borrows.json*", "*/extracted/*", "*.partial.txt",
    "*.chapters.json", "*.png", "*.jpg", "*.jpeg", "*.har", "*scratch/*", "*.playwright-mcp*",
    "*browser-profile*", "*__pycache__*", "*.pyc",
]


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build artifacts")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([uv, "build", "--out-dir", str(out), str(ROOT)], check=True,
                   capture_output=True, text=True)
    return out


@pytest.fixture(scope="module")
def wheel(dist) -> zipfile.ZipFile:
    (path,) = dist.glob("*.whl")
    return zipfile.ZipFile(path)


@pytest.fixture(scope="module")
def sdist_names(dist) -> list[str]:
    (path,) = dist.glob("*.tar.gz")
    with tarfile.open(path) as tar:
        return tar.getnames()


def _dist_info(wheel: zipfile.ZipFile, name: str) -> str:
    (member,) = [n for n in wheel.namelist() if n.endswith(f".dist-info/{name}")]
    return wheel.read(member).decode()


def test_wheel_names_match_distribution(dist):
    (path,) = dist.glob("*.whl")
    assert path.name.startswith("research_engine_plugin_yourcloudlibrary-0.3.0-py3-none-any")


def test_wheel_ships_manifest_and_every_entry_module(wheel):
    names = set(wheel.namelist())
    assert "ycl/plugin.yaml" in names
    manifest = yaml.safe_load(wheel.read("ycl/plugin.yaml"))
    entries = [t["entry"] for t in manifest["provides"]["mcp_tools"]]
    entries += [p["entry"] for p in manifest["provides"]["source_search"]]
    for entry in entries:
        module = entry.partition(":")[0]
        assert f"{module.replace('.', '/')}.py" in names, entry


def test_wheel_contains_only_the_plugin_package(wheel):
    top_level = {name.split("/", 1)[0] for name in wheel.namelist()}
    assert top_level == {"ycl", "research_engine_plugin_yourcloudlibrary-0.3.0.dist-info"}


def test_no_sensitive_or_local_files_in_artifacts(wheel, sdist_names):
    for name in [*wheel.namelist(), *sdist_names]:
        for pattern in FORBIDDEN:
            assert not fnmatch.fnmatch(name, pattern), f"{name} matches {pattern}"


def test_sdist_is_allow_listed(sdist_names):
    top = {name.split("/", 2)[1] for name in sdist_names if name.count("/") >= 1}
    # Hatchling always adds .gitignore and PKG-INFO.
    assert top <= {"ycl", "tests", "README.md", "LICENSE", "CHANGELOG.md", "pyproject.toml",
                   "PKG-INFO", ".gitignore"}
    assert not any("/scripts/" in name or name.endswith("IMPL_NOTES.md") for name in sdist_names)


def test_entry_points_advertise_plugin_and_login(wheel):
    entry_points = _dist_info(wheel, "entry_points.txt")
    assert "[research_engine.plugins]\nyourcloudlibrary = ycl" in entry_points
    assert "research-engine-ycl-login = ycl.cli.login:main" in entry_points


def test_metadata_is_complete(wheel):
    meta = email.parser.Parser().parsestr(_dist_info(wheel, "METADATA"))
    assert meta["Name"] == "research-engine-plugin-yourcloudlibrary"
    assert meta["Version"] == "0.3.0"
    assert meta["Requires-Python"] == ">=3.11"
    assert meta["License-Expression"] == "Apache-2.0"
    assert meta.get_all("License-File") == ["LICENSE"]
    assert meta["Description-Content-Type"] == "text/markdown"
    requires = meta.get_all("Requires-Dist")
    assert any(r.replace(" ", "").startswith("research-engine-sdk") and "<0.7" in r and ">=0.6" in r
               for r in requires)
    assert not any(r.split("[")[0].split(";")[0].strip().startswith("research-engine")
                   and not r.startswith("research-engine-sdk") for r in requires)
    urls = dict(u.split(", ", 1) for u in meta.get_all("Project-URL"))
    assert {"Homepage", "Source", "Issues", "Changelog"} <= set(urls)
