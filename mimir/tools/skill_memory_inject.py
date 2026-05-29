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


def _skill_from_path(path: str) -> str | None:
    """Skill name for a ``.../<skill>/SKILL.md`` path, else ``None``.

    The skill name is the immediate parent directory of ``SKILL.md`` —
    the same identifier deepagents' SkillsMiddleware shows in its catalog
    and that the write path (``saga_record_skill_learning``) records
    under. A bare ``SKILL.md`` with no parent dir yields ``None`` (can't
    be scoped).
    """
    if not path:
        return None
    norm = path.replace("\\", "/").rstrip("/")
    parts = norm.split("/")
    if len(parts) < 2 or parts[-1] != _SKILL_FILENAME:
        return None
    skill = parts[-2].strip()
    return skill or None


def _resolve_conn() -> Any | None:
    """Best-effort sqlite conn from the installed SagaStore.

    ``set_memory_client`` receives the raw concrete ``SagaStore`` (the
    peeling in ``agent._try_inject_memory_client`` unwraps any recording/
    proxy layers), so ``client.connection()`` (added in slice 2) hands
    back the live connection directly. ``None`` if memory isn't wired."""
    try:
        from .memory import _MEMORY_STATE
        client = _MEMORY_STATE.get("client")
        if client is None:
            return None
        conn_fn = getattr(client, "connection", None)
        if conn_fn is None:
            return None
        return conn_fn()
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
    file_path: str, content: Any, conn: Any
) -> "tuple[str, list[str]] | None":
    """Return ``(augmented_content, atom_ids)`` or ``None`` to leave unchanged.

    Pure/sync so the async path can offload it to a thread (the SQL in
    ``augment_skill_body`` shouldn't run on the event loop).

    Returns ``None`` when the path doesn't resolve to a skill, no learnings
    exist, or any error occurs — the caller leaves the read result unchanged
    in those cases.  Callers use the returned atom_ids to credit the
    injected learning atoms on the turn's votable set (chainlink #266
    slice 6)."""
    skill = _skill_from_path(file_path)
    if skill is None or conn is None or not isinstance(content, str):
        return None
    try:
        from .. import skill_memory
        augmented, atom_ids = skill_memory.augment_skill_body(conn, skill, content)
    except Exception:  # noqa: BLE001 — never break a file read
        return None
    if augmented == content:
        return None
    return augmented, atom_ids


def _credit_skill_atom_ids(atom_ids: list[str]) -> None:
    """Extend the current turn's votable atom set with *atom_ids*.

    The synthesis turn scores every ``atom_id`` on the turn's votable set
    via ``saga_feedback``.  By adding the injected skill-learning atom IDs
    here, those atoms join the voting loop: useful learnings accrue weight-
    2.0 feedback_positive events and rise in activation-based recall; stale
    ones get marked and decay out.

    Best-effort: no TurnContext in context (e.g. tests, synthetic channels)
    → silently does nothing.  Duplicate IDs are deduplicated against the
    existing set so an atom can't be added twice.
    """
    if not atom_ids:
        return
    try:
        from .._context import get_current_turn
        ctx = get_current_turn()
        if ctx is None:
            return
        existing = set(ctx.saga_atom_ids)
        ctx.saga_atom_ids.extend(aid for aid in atom_ids if aid not in existing)
    except Exception:  # noqa: BLE001 — never interfere with the tool call
        pass


class SkillMemoryInjectionMiddleware(AgentMiddleware):
    """Append a skill's learnings to a ``read_file`` of its ``SKILL.md``.

    Pairs with the poller-turn ``auto_skill_block`` injection (slice 2);
    this covers the non-poller path where the model loads a skill by
    reading the file itself.
    """

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        if _tool_name(request) != _READ_FILE_TOOL or not _is_success_text(result):
            return result
        outcome = _compute_augmented(
            _file_path_arg(request), result.content, _resolve_conn(),
        )
        if outcome is not None:
            augmented_body, atom_ids = outcome
            result.content = augmented_body
            _credit_skill_atom_ids(atom_ids)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if _tool_name(request) != _READ_FILE_TOOL or not _is_success_text(result):
            return result
        outcome = await asyncio.to_thread(
            _compute_augmented,
            _file_path_arg(request), result.content, _resolve_conn(),
        )
        if outcome is not None:
            augmented_body, atom_ids = outcome
            result.content = augmented_body
            _credit_skill_atom_ids(atom_ids)
        return result
