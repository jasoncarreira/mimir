"""Non-poller skill-memory load injection (chainlink #266, slice 3).

Poller turns inject a skill's learnings via ``auto_skill_block`` in
agent.py (slice 2). On NON-poller turns the model loads a skill by
calling ``read_file`` on its ``<skill>/SKILL.md`` (per deepagents'
SkillsMiddleware instructions). This middleware intercepts that read and
appends the skill's recorded learnings — gotchas, input quirks, tips
from past runs — to the returned content via the same
``skill_memory.augment_skill_body`` the poller path uses, so the model
sees them inline the moment it opens the skill.

Best-effort throughout: any failure (no SagaStore installed, an
unparseable path, a DB error) returns the read result UNCHANGED. The
middleware never blocks a read, never changes a non-``read_file`` call,
and only touches successful text reads whose path ends in ``SKILL.md``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

log = logging.getLogger(__name__)

_READ_FILE_TOOL = "read_file"
_SKILL_FILENAME = "SKILL.md"


def _tool_name(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    return str(tc.get("name") or "")


def _file_path_arg(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    args = tc.get("args") or {}
    return str(args.get("file_path") or "")


def _skill_from_path(path: str, skill_sources: tuple[Path, ...]) -> str | None:
    """Registered skill name for a ``<source>/<skill>/SKILL.md`` path.

    The resolved file must be an immediate child of one of the same source
    roots registered with deepagents. This prevents an arbitrary model-named
    directory (and symlink escapes) from selecting a learning set.
    """
    if not path or not skill_sources:
        return None
    try:
        candidate = Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if candidate.name != _SKILL_FILENAME:
        return None
    for source in reversed(skill_sources):
        try:
            relative = candidate.relative_to(source)
        except ValueError:
            continue
        if len(relative.parts) == 2 and relative.parts[-1] == _SKILL_FILENAME:
            skill = relative.parts[0].strip()
            return skill or None
    return None


def _resolve_client() -> Any | None:
    """Best-effort handle to the installed SagaStore.

    ``set_memory_client`` receives the raw concrete ``SagaStore`` (the
    peeling in ``agent._try_inject_memory_client`` unwraps any recording/
    proxy layers). chainlink #411: we hand back the *store* — not its raw
    connection — so ``_compute_augmented`` can run the recall through
    ``run_locked_read``, the store's own serialization, instead of
    touching the shared ``check_same_thread=False`` connection from a
    bare worker thread (the cross-thread access SagaStore's threading
    contract forbids; #365/#386). ``None`` if memory isn't wired or the
    client doesn't expose the helper."""
    try:
        from .memory import _MEMORY_STATE
        client = _MEMORY_STATE.get("client")
        if client is None:
            return None
        if getattr(client, "run_locked_read", None) is None:
            return None
        return client
    except Exception:  # noqa: BLE001 — injection is best-effort
        return None


def _is_success_text(result: Any) -> bool:
    """True if *result* is a successful ``read_file`` ToolMessage carrying
    string content (skip non-text content_blocks, errors, empties)."""
    return (
        isinstance(result, ToolMessage)
        and getattr(result, "status", None) != "error"
        and isinstance(result.content, str)
        and bool(result.content.strip())
    )


def _compute_augmented(
    file_path: str, content: Any, client: Any, skill_sources: tuple[Path, ...],
) -> tuple[str | None, list[str], list[dict[str, str | None]]]:
    """Return ``(augmented_content_or_None, injected_atom_ids, ifc_sources)``.

    ``None`` for the content leaves the read unchanged. The atom IDs are
    recorded onto the turn for synthesis feedback, while ``ifc_sources``
    carries the same selected atoms into the turn's IFC carrier. Sync
    so the async path can offload it to a thread; the SQL runs inside
    ``client.run_locked_read`` so the shared connection is never touched
    cross-thread without the store's lock (chainlink #411)."""
    skill = _skill_from_path(file_path, skill_sources)
    if skill is None or client is None or not isinstance(content, str):
        return None, [], []
    try:
        from .._context import get_current_turn

        ctx = get_current_turn()
        auth_context = getattr(ctx, "auth_context", None) if ctx else None
    except Exception:  # noqa: BLE001 — context resolution is best-effort
        auth_context = None
    try:
        from .. import skill_memory
        augmented, ids, ifc_sources = client.run_locked_read(
            lambda conn: skill_memory.augment_skill_body(
                conn, skill, content, auth_context=auth_context
            )
        )
    except Exception:  # noqa: BLE001 — never break a file read
        return None, [], []
    if augmented == content:
        return None, [], []
    return augmented, ids, ifc_sources


def _record_injected_memory(
    ids: list[str], ifc_sources: list[dict[str, str | None]],
) -> None:
    """Best-effort: record injected atom IDs and merge their IFC provenance."""
    if not ids:
        return
    try:
        from .._context import get_current_turn
        ctx = get_current_turn()
        bucket = getattr(ctx, "injected_skill_atom_ids", None)
        if bucket is None:
            return
        for aid in ids:
            if aid not in bucket:
                bucket.append(aid)
        auth_context = getattr(ctx, "auth_context", None)
        if auth_context is None or not ifc_sources:
            return
        from ..agent import _auto_recall_source_labels, _merge_ifc_labels

        added = _auto_recall_source_labels(
            auth_context, {"_ifc_sources": ifc_sources},
        )
        merged = auth_context.ifc_state.merge(added, fallback=ctx.ifc_labels)
        ctx.ifc_labels = merged
        ctx.auth_context = replace(auth_context, ifc_labels=merged)
    except Exception:  # noqa: BLE001 — injection bookkeeping is best-effort
        pass


class SkillMemoryInjectionMiddleware(AgentMiddleware):
    """Append a skill's learnings to a ``read_file`` of its ``SKILL.md``.

    Pairs with the poller-turn ``auto_skill_block`` injection (slice 2);
    this covers the non-poller path where the model loads a skill by
    reading the file itself.
    """

    def __init__(self, skill_sources: list[str] | None = None) -> None:
        self.set_skill_sources(skill_sources or [])

    def set_skill_sources(self, skill_sources: list[str]) -> None:
        """Refresh the roots shared with deepagents' live skill catalog."""
        self._skill_sources = tuple(
            Path(source).resolve(strict=False) for source in skill_sources
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        if _tool_name(request) != _READ_FILE_TOOL or not _is_success_text(result):
            return result
        augmented, ids, ifc_sources = _compute_augmented(
            _file_path_arg(request), result.content, _resolve_client(),
            self._skill_sources,
        )
        if augmented is not None:
            result.content = augmented
            _record_injected_memory(ids, ifc_sources)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if _tool_name(request) != _READ_FILE_TOOL or not _is_success_text(result):
            return result
        augmented, ids, ifc_sources = await asyncio.to_thread(
            _compute_augmented,
            _file_path_arg(request), result.content, _resolve_client(),
            self._skill_sources,
        )
        if augmented is not None:
            result.content = augmented
            _record_injected_memory(ids, ifc_sources)
        return result
