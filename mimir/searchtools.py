"""``file_search`` + ``rebuild_index`` tools (SPEC §8.1, §8.3).

These are MCP tools (in-process) closing over a shared ``Indexer``. The
spec frames them as Claude Agent SDK skills; at the model interface a skill
that exposes a function is just a tool, so for v1 they live alongside the
file-op tools (SPEC §1.5).
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._tool_helpers import _ArgError, _content_block, _need, _safe
from .search import Indexer


def build_search_tools(indexer: Indexer) -> list[SdkMcpTool]:
    @tool(
        "file_search",
        "Hybrid semantic + keyword search over memory/ and state/ files. "
        "Use when you need to find a file by topic, not by exact path. "
        "If you know the path, call read_file directly instead. "
        "Returns up to k results with path, score, snippet, description.",
        {"query": str, "scope": str, "k": int},
    )
    @_safe("file_search", param_names=["query", "scope", "k"])
    async def file_search(args: dict[str, Any]) -> dict[str, Any]:
        query = _need(args, "query")
        scope = (args.get("scope") or "all").strip().lower()
        if scope not in ("memory", "state", "all"):
            return _content_block(
                f"file_search failed: scope must be one of memory/state/all (got {scope!r})",
                is_error=True,
            )
        k_raw = args.get("k", 5)
        try:
            k = max(1, min(int(k_raw), 20))
        except (TypeError, ValueError):
            k = 5
        results = await indexer.search(query, scope=scope, k=k)
        if not results:
            return _content_block("(no matches)")
        payload = [r.to_dict() for r in results]
        return _content_block(json.dumps(payload, indent=2, ensure_ascii=False))

    @tool(
        "rebuild_index",
        "Force a full reindex + INDEX.md regeneration. Normally unnecessary — "
        "indexes auto-rebuild on file writes and on a 60s mtime sweep. Use "
        "this when files arrive out-of-band or you want to confirm freshness. "
        "Returns added/updated/removed counts.",
        {"scope": str},
    )
    @_safe("rebuild_index", param_names=["scope"])
    async def rebuild_index(args: dict[str, Any]) -> dict[str, Any]:
        # ``scope`` is accepted for forward-compat with §8.3's signature, but
        # the indexer always sweeps both trees at once (cheap).
        scope = (args.get("scope") or "all").strip().lower()
        if scope not in ("memory", "state", "all"):
            return _content_block(
                f"rebuild_index failed: scope must be one of memory/state/all (got {scope!r})",
                is_error=True,
            )
        stats = await indexer.sweep()
        return _content_block(
            f"rebuild_index ok: added={stats['added']} updated={stats['updated']} "
            f"removed={stats['removed']}"
        )

    return [file_search, rebuild_index]


def search_tool_names() -> list[str]:
    return ["mcp__mimir__file_search", "mcp__mimir__rebuild_index"]
