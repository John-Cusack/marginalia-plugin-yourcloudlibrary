"""Shared fixtures, and the SDK the tests run against.

Unit and contract tests need only ``research_engine_sdk`` and the plugin — never
core. Until ``research-engine-sdk`` 0.6 is published, ``tests/_sdk_standin`` stands
in for it; an installed ``research-engine-sdk>=0.6`` always wins. Set
``YCL_TEST_REQUIRE_REAL_SDK=1`` (release CI does) to fail instead of falling back.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path

import pytest

_STANDIN = Path(__file__).parent / "_sdk_standin"
_MIN_SDK = (0, 6)


def _installed_sdk_version() -> str | None:
    try:
        return importlib.metadata.version("research-engine-sdk")
    except importlib.metadata.PackageNotFoundError:
        return None


def _is_supported(version: str) -> bool:
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= _MIN_SDK


_SDK_VERSION = _installed_sdk_version()
USING_STANDIN_SDK = _SDK_VERSION is None or not _is_supported(_SDK_VERSION)

if USING_STANDIN_SDK:
    if os.environ.get("YCL_TEST_REQUIRE_REAL_SDK") == "1":
        raise RuntimeError(
            f"research-engine-sdk>=0.6 is required (found {_SDK_VERSION or 'none'})."
        )
    sys.path.insert(0, str(_STANDIN))


def pytest_report_header(config) -> str:
    if USING_STANDIN_SDK:
        return (
            "research_engine_sdk: TEST STAND-IN (tests/_sdk_standin) — install "
            "research-engine-sdk>=0.6 to test against the real contract"
        )
    return f"research_engine_sdk: {_SDK_VERSION}"


@pytest.fixture
def plugin_context(tmp_path):
    """A ``PluginContext`` whose data directory is an empty temp dir."""
    from research_engine_sdk import PluginContext

    return PluginContext(
        plugin_id="yourcloudlibrary",
        data_dir=tmp_path / "plugin-data",
        distribution_name="research-engine-plugin-yourcloudlibrary",
        distribution_version="0.3.0",
    )


@pytest.fixture
def plugin_paths(plugin_context):
    from ycl._paths import resolve_paths

    return resolve_paths(plugin_context)
