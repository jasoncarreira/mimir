"""Cultivate's forge tool surface during the core-import compatibility period.

The legacy implementation remains the single source of scope resolution, head
checks, identity binding and result provenance until its importers are migrated.
These are distinct tool instances with the same schemas and execution path;
registration can switch to this surface without changing tool-call contracts.
"""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any

from langchain_core.tools import StructuredTool

from mimir.tools import forge as _legacy


def _wrap_tool(source: StructuredTool) -> StructuredTool:
    if source.func is None:
        raise RuntimeError(f"forge tool {source.name!r} has no synchronous implementation")

    @wraps(source.func)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        return source.func(*args, **kwargs)

    @wraps(source.func)
    async def invoke_async(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(invoke, *args, **kwargs)

    return StructuredTool.from_function(
        func=invoke,
        coroutine=invoke_async,
        name=source.name,
        description=source.description,
        args_schema=source.args_schema,
        infer_schema=False,
        return_direct=source.return_direct,
        response_format=source.response_format,
    )


FORGE_TOOLS: tuple[StructuredTool, ...] = tuple(
    _wrap_tool(source) for source in _legacy.FORGE_TOOLS
)
