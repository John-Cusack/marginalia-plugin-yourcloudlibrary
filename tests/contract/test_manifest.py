"""ycl/plugin.yaml is the runtime contract core audits before import. Keep it true.

Checks it against the SDK manifest model, the package metadata core discovers it
through, and the code it describes (entries, tool schemas, provider protocol,
network hosts).
"""

from __future__ import annotations

import importlib
import importlib.resources
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def raw() -> dict:
    text = importlib.resources.files("ycl").joinpath("plugin.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _entry(ref: str):
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


def test_validates_against_sdk_manifest_model(raw):
    import research_engine_sdk

    model = getattr(research_engine_sdk, "PluginManifest", None)
    if model is None:
        from research_engine_sdk.manifest import PluginManifest as model
    manifest = model.model_validate(raw)
    assert manifest.schema_version == 2


def test_identity_lives_in_package_metadata_not_the_manifest(raw, pyproject):
    for key in ("name", "version", "author", "description", "license", "homepage"):
        assert key not in raw
    assert "pip" not in raw["requires"]
    assert "setup_commands" not in raw["requires"]
    assert not (ROOT / "pack.yaml").exists(), "one authoritative manifest only"
    entry_points = pyproject["project"]["entry-points"]["research_engine.plugins"]
    assert entry_points == {raw["plugin_id"]: "ycl"}
    assert raw["plugin_id"] == "yourcloudlibrary"


def test_core_and_python_ranges(raw, pyproject):
    assert raw["requires"]["core_api"] == ">=0.6,<0.7"
    assert raw["requires"]["python"] == pyproject["project"]["requires-python"]
    assert "research-engine-sdk>=0.6,<0.7" in pyproject["project"]["dependencies"]
    assert not any(
        re.match(r"research-engine(?!-sdk)\b", dep) for dep in pyproject["project"]["dependencies"]
    ), "plugins depend on the SDK, never on core"


def test_tools_are_namespaced_importable_and_schemas_match_decorators(raw):
    tools = raw["provides"]["mcp_tools"]
    ids = [tool["id"] for tool in tools]
    assert len(ids) == len(set(ids)) == 10
    for tool in tools:
        assert tool["id"].startswith(f"{raw['plugin_id']}.")
        assert tool["entry"].startswith("ycl.tools.")
        handler = _entry(tool["entry"])
        assert handler._tool_id == tool["id"]
        assert handler._tool_input_schema == tool["input_schema"], tool["id"]


def test_every_tool_schema_is_complete(raw):
    for tool in raw["provides"]["mcp_tools"]:
        schema = tool["input_schema"]
        assert schema["type"] == "object", tool["id"]
        properties = schema["properties"]
        assert set(schema.get("required", [])) <= set(properties), tool["id"]
        for name, prop in properties.items():
            assert "type" in prop and prop.get("description"), f"{tool['id']}.{name}"


def test_document_type_and_source_search_provider(raw):
    from research_engine_sdk import SourceSearchProvider

    from ycl.tools._ingest import DOCUMENT_TYPE

    assert raw["provides"]["document_types"] == [
        {"id": DOCUMENT_TYPE, "default_chunker": "prose_window"}
    ]
    (provider_entry,) = raw["provides"]["source_search"]
    provider_cls = _entry(provider_entry["entry"])
    provider = provider_cls()
    assert isinstance(provider, SourceSearchProvider)
    assert provider.plugin_name == raw["plugin_id"]


def test_permissions_cover_every_host_the_code_contacts(raw):
    from ycl.api import client
    from ycl.cli import login

    permissions = raw["permissions"]
    assert permissions["network"] == "egress"
    assert permissions["ingest"] is True
    assert permissions["subprocess"] is True  # Chromium for login + catalog search
    assert permissions["filesystem"] == "plugin_data"
    code_hosts = {
        urlsplit(url).hostname
        for url in (client.EBOOK_HOST, client.EPUBSERVICE_HOST, login.DEFAULT_START_URL,
                    login.LIBRARY_START_URL)
    }
    assert code_hosts == set(permissions["network_allowlist"])
