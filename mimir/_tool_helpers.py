"""Small shared helpers for tool handlers (MCP-side error formatting).

Used by saga, search, and schedule MCP tools — kept generic so any future
in-process tool can wrap arg validation and consistent error responses.
"""

from __future__ import annotations

from typing import Any


class _ArgError(ValueError):
    """Raised by ``_need`` and converted to is_error responses by ``_safe``."""


def _content_block(text: str, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def _need(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or val == "":
        raise _ArgError(f"argument {key!r} is required and must be a non-empty string")
    return val


def _safe(tool_name: str):
    """Wrap a tool handler so ``_ArgError`` is converted to is_error blocks."""

    def deco(fn):
        async def wrapper(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await fn(args)
            except _ArgError as exc:
                return _content_block(f"{tool_name} failed: {exc}", is_error=True)

        wrapper.__name__ = fn.__name__
        wrapper.__qualname__ = fn.__qualname__
        return wrapper

    return deco
