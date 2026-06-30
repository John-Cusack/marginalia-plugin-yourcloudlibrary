"""Shared test fixtures + a stub for the host ``research_engine`` SDK.

The plugin's MCP tool handlers ``import research_engine.plugins.sdk`` (and the
ingestion chunker), which only exists inside the Marginalia host at runtime.
To unit-test the handlers in isolation we register lightweight stub modules in
``sys.modules`` before they're imported. The ``tool`` decorator becomes a
pass-through so the wrapped coroutine stays directly callable.
"""

from __future__ import annotations

import sys
import types


def _install_research_engine_stub() -> None:
    if "research_engine" in sys.modules:
        return

    research_engine = types.ModuleType("research_engine")
    plugins = types.ModuleType("research_engine.plugins")
    sdk = types.ModuleType("research_engine.plugins.sdk")

    def tool(*_args, **_kwargs):
        """Pass-through stand-in for the host ``@tool`` decorator."""

        def decorator(fn):
            return fn

        return decorator

    sdk.tool = tool
    plugins.sdk = sdk
    research_engine.plugins = plugins

    # Ingestion chunker used by ycl.tools.ingest_book.
    services = types.ModuleType("research_engine.services")
    ingestion = types.ModuleType("research_engine.services.ingestion")
    chunking = types.ModuleType("research_engine.services.ingestion.chunking")
    prose_window = types.ModuleType(
        "research_engine.services.ingestion.chunking.prose_window"
    )

    class ProseWindowChunker:  # pragma: no cover - lightweight stand-in
        """Faithful-enough stand-in for the host chunker.

        Mirrors the real contract the handler relies on: it accepts window
        kwargs and yields passage-draft-like objects with a *settable*
        ``position`` and a readable ``metadata`` (the host returns pydantic
        ``PassageDraft`` objects; ``_chunk_with_chapters`` renumbers
        ``draft.position`` across chapters). It also splits long text into
        several windows so multi-passage paths are actually exercised.
        """

        def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50) -> None:
            self.max_tokens = max_tokens
            self.overlap_tokens = overlap_tokens

        async def chunk(self, text, metadata=None):
            if not text or not text.strip():
                return []
            window = max(1, self.max_tokens * 4)  # ~4 chars/token, like the real one
            pieces = [text[i : i + window] for i in range(0, len(text), window)]
            return [
                types.SimpleNamespace(
                    text=piece,
                    position=i,
                    metadata=metadata or {},
                    token_count=max(1, len(piece) // 4),
                    locator={"byte_start": 0, "byte_end": len(piece.encode())},
                )
                for i, piece in enumerate(pieces)
            ]

    prose_window.ProseWindowChunker = ProseWindowChunker
    chunking.prose_window = prose_window
    ingestion.chunking = chunking
    services.ingestion = ingestion
    research_engine.services = services

    for name, module in {
        "research_engine": research_engine,
        "research_engine.plugins": plugins,
        "research_engine.plugins.sdk": sdk,
        "research_engine.services": services,
        "research_engine.services.ingestion": ingestion,
        "research_engine.services.ingestion.chunking": chunking,
        "research_engine.services.ingestion.chunking.prose_window": prose_window,
    }.items():
        sys.modules[name] = module


_install_research_engine_stub()
