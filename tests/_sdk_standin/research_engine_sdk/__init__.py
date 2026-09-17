"""TEST-ONLY stand-in for ``research-engine-sdk`` 0.6 — delete when 0.6.0 is published.

This is NOT the SDK. It reproduces only the slice of the 0.6 contract this plugin
consumes, following the SDK as specified (MarginaliaAI
``docs/implementation/pypi-plugin-migration/core-sdk-history.md`` §1) and as
drafted in core's in-progress ``packages/sdk`` (2026-09-17): ``PluginContext``,
``IngestionClient.ingest_document``, the source-search DTOs, ``@tool``, and the
manifest-v2 rules the plugin must satisfy (strict keys, ``plugin_id``-namespaced
tool ids, ``plugin_data`` filesystem permission).

``tests/conftest.py`` puts it on ``sys.path`` only when no
``research-engine-sdk>=0.6`` is installed, and the pytest header says which one is
in use. Anything here that disagrees with the published SDK is a bug in this file,
not a contract: the real package always wins when installed.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__version__ = "0.6.0+standin"

# --- decorators --------------------------------------------------------------


def tool(id: str, description: str, input_schema: dict[str, Any] | None = None) -> Callable:
    """Mark an async function as an MCP tool (metadata only)."""

    def decorator(fn: Callable) -> Callable:
        schema = input_schema or {"type": "object", "properties": {}}

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await fn(*args, **kwargs)

        wrapper._tool_id = id  # type: ignore[attr-defined]
        wrapper._tool_description = description  # type: ignore[attr-defined]
        wrapper._tool_input_schema = schema  # type: ignore[attr-defined]
        return wrapper

    return decorator


# --- context -----------------------------------------------------------------


class PluginContext(BaseModel):
    plugin_id: str
    data_dir: Path
    distribution_name: str
    distribution_version: str


# --- errors ------------------------------------------------------------------


class PermissionDenied(Exception):
    pass


class PluginConfigError(Exception):
    pass


# --- source search -----------------------------------------------------------


class Availability(enum.StrEnum):
    in_corpus = "in_corpus"
    ingestable = "ingestable"
    borrowable = "borrowable"
    purchasable = "purchasable"
    external_only = "external_only"


class SourceQuery(BaseModel):
    query: str
    title: str | None = None
    author: str | None = None
    year: int | None = None
    doi: str | None = None
    isbn: str | None = None
    asin: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class IngestAction(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class SourceMatch(BaseModel):
    plugin: str
    source_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    availability: Availability
    confidence: float = Field(ge=0.0, le=1.0)
    ingest_action: IngestAction | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class SourceSearchProvider(Protocol):
    @property
    def plugin_name(self) -> str: ...

    async def search(self, query: SourceQuery, *, limit: int) -> list[SourceMatch]: ...


# --- clients -----------------------------------------------------------------


@runtime_checkable
class IngestionClient(Protocol):
    async def ingest_document(
        self,
        *,
        title: str,
        document_type: str,
        text: str,
        source: str = "",
        metadata: dict | None = None,
        language: str | None = None,
        sections: list[dict] | None = None,
    ) -> dict: ...

    async def find_existing(
        self, *, source: str | None = None, source_pattern: str | None = None
    ) -> list[dict]: ...


# --- manifest v2 -------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Requires(_Strict):
    core_api: str
    python: str = ">=3.11"


class Permissions(_Strict):
    network: Literal["none", "egress", "full"] = "none"
    network_allowlist: list[str] = Field(default_factory=list)
    filesystem: Literal["none", "plugin_data", "read_corpus", "read_write_plugin_data"] = "none"
    llm: bool = False
    ingest: bool = False
    write: bool = False
    subprocess: bool = False

    @model_validator(mode="after")
    def _allowlist_requires_network(self) -> Permissions:
        if self.network == "none" and self.network_allowlist:
            raise ValueError("network_allowlist requires network permission")
        return self


class DocumentType(_Strict):
    id: str
    default_chunker: str = "prose_window"


class ToolContribution(_Strict):
    id: str
    entry: str
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]

    @field_validator("input_schema")
    @classmethod
    def _object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("tool input_schema must have type: object")
        return value


class SourceSearchContribution(_Strict):
    id: str
    entry: str
    description: str = Field(min_length=1)


class Provides(_Strict):
    document_types: list[DocumentType] = Field(default_factory=list)
    mcp_tools: list[ToolContribution] = Field(default_factory=list)
    source_search: list[SourceSearchContribution] = Field(default_factory=list)


class PluginManifest(_Strict):
    schema_version: Literal[2]
    plugin_id: str
    requires: Requires
    permissions: Permissions = Field(default_factory=Permissions)
    provides: Provides = Field(default_factory=Provides)

    @model_validator(mode="after")
    def _namespaced_tools(self) -> PluginManifest:
        for contribution in self.provides.mcp_tools:
            if not contribution.id.startswith(f"{self.plugin_id}."):
                raise ValueError(
                    f"tool id {contribution.id!r} must be namespaced by {self.plugin_id!r}"
                )
        return self


__all__ = [
    "Availability",
    "IngestAction",
    "IngestionClient",
    "PermissionDenied",
    "PluginConfigError",
    "PluginContext",
    "PluginManifest",
    "SourceMatch",
    "SourceQuery",
    "SourceSearchProvider",
    "tool",
]
