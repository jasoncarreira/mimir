"""Representative tool ports for the migration coverage matrix.

Three distinct patterns from mimir's existing tool surface translated
to LangChain @tool. After these the remaining tools (channeltools,
scheduletools, committools, spawn) are mechanical clones of the same
patterns.

Patterns covered:
  1. Filter-args + dependency injection (mimir/searchtools.py)
  2. JSONL read with structured-shape return (mimir/turntools.py)
  3. Subprocess execution with permission scoping (mimir/shelltools.py)

Each tool's *function-level* dependencies (Indexer, turns_log_path,
shell allowlist) injected via module-level setter functions, parallel
to memory_tool.py's set_memory_client pattern. Production cutover
would wire these in the deepagent factory.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import tool


# ────────────────────────────────────────────────────────────────────
# Pattern 1: file_search — filter args + Indexer dep injection
# ────────────────────────────────────────────────────────────────────

_SEARCH_STATE: dict[str, Any] = {"indexer": None}


def set_indexer(indexer: Any) -> None:
    """Inject the Indexer the file_search tool will query against."""
    _SEARCH_STATE["indexer"] = indexer


@tool
async def file_search(
    query: str,
    scope: str = "all",
    k: int = 5,
) -> str:
    """Hybrid semantic + keyword search over memory/ and state/ files.

    Use when you need to find a file by topic, not by exact path. If
    you know the path, call read_file directly instead. Returns up
    to k results with path, score, snippet, description.

    Args:
        query: Natural-language search query.
        scope: One of ``"memory"``, ``"state"``, or ``"all"`` (default).
        k: Max results to return (1-20, default 5).

    Returns:
        JSON-formatted list of matches, or "(no matches)" if empty.
    """
    indexer = _SEARCH_STATE["indexer"]
    if indexer is None:
        return "file_search failed: no Indexer configured"
    scope_clean = (scope or "all").strip().lower()
    if scope_clean not in ("memory", "state", "all"):
        return (
            f"file_search failed: scope must be one of memory/state/all "
            f"(got {scope_clean!r})"
        )
    try:
        k_clean = max(1, min(int(k), 20))
    except (TypeError, ValueError):
        k_clean = 5
    results = await indexer.search(query, scope=scope_clean, k=k_clean)
    if not results:
        return "(no matches)"
    payload = [r.to_dict() for r in results]
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ────────────────────────────────────────────────────────────────────
# Pattern 2: mimir_get_turn — JSONL read, structured return
# ────────────────────────────────────────────────────────────────────

_TURN_STATE: dict[str, Optional[Path]] = {"turns_log_path": None}


def set_turns_log_path(path: Path) -> None:
    """Inject the path to turns.jsonl."""
    _TURN_STATE["turns_log_path"] = path


@tool
def mimir_get_turn(turn_id: str) -> str:
    """Retrieve the full record for one previous agent turn by its ID.

    The agent's per-turn telemetry (tool calls, tool results, output)
    lives in ``turns.jsonl``. Use this to dig into what happened on a
    specific past turn — for debugging, reflection, or auditing.

    Args:
        turn_id: The 12-char hex turn_id to retrieve.

    Returns:
        JSON-formatted turn record, with the ``input`` field stripped
        (it's a derived prompt-render — preserve context budget). Or
        an error if not found.
    """
    if not turn_id or not turn_id.strip():
        return "mimir_get_turn failed: turn_id is required"
    path = _TURN_STATE["turns_log_path"]
    if path is None:
        return "mimir_get_turn failed: turns log path not configured"
    if not path.exists():
        return f"mimir_get_turn failed: turns log not found at {path}"
    target = turn_id.strip()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip malformed lines
            if row.get("turn_id") == target:
                # Strip input + saga_atom_ids + usage (large fields not
                # needed for the agent's debugging use case)
                for k in ("input", "saga_atom_ids", "usage"):
                    row.pop(k, None)
                return json.dumps(row, indent=2, ensure_ascii=False)
    return f"mimir_get_turn: no turn found with id {target!r}"


# ────────────────────────────────────────────────────────────────────
# Pattern 3: shell_exec — subprocess execution + permission scoping
# ────────────────────────────────────────────────────────────────────

_SHELL_STATE: dict[str, Any] = {
    "allowlist": None,  # set of allowed command prefixes; None = anything
    "cwd": None,
    "timeout_s": 60.0,
}


def set_shell_allowlist(
    allowlist: list[str] | None,
    *,
    cwd: Path | None = None,
    timeout_s: float = 60.0,
) -> None:
    """Configure shell_exec safety bounds.

    Args:
        allowlist: List of allowed command prefixes (e.g. ``["git ",
            "ls", "rg "]``). Tool refuses commands not matching any
            prefix. ``None`` disables allowlist (testing / trusted
            contexts).
        cwd: Working directory for command execution. ``None`` uses
            process cwd.
        timeout_s: Subprocess timeout in seconds (default 60).
    """
    _SHELL_STATE["allowlist"] = (
        set(allowlist) if allowlist is not None else None
    )
    _SHELL_STATE["cwd"] = cwd
    _SHELL_STATE["timeout_s"] = timeout_s


@tool
def shell_exec(command: str) -> str:
    """Execute a shell command and return stdout + stderr + exit code.

    Subject to an operator-configured allowlist of command prefixes
    (see ``set_shell_allowlist``). Always runs without shell-expansion
    (``shell=False``) via ``shlex.split`` to prevent injection.

    Args:
        command: The full command line (will be split via shlex).

    Returns:
        Formatted block: stdout, stderr, exit code.
    """
    if not command or not command.strip():
        return "shell_exec failed: command is required"
    allowlist = _SHELL_STATE["allowlist"]
    if allowlist is not None:
        if not any(command.startswith(prefix) for prefix in allowlist):
            return (
                f"shell_exec rejected: '{command[:80]}...' does not match "
                f"any allowlist prefix"
            )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"shell_exec failed: shell-parse error: {exc}"
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SHELL_STATE["timeout_s"],
            cwd=_SHELL_STATE["cwd"],
        )
    except subprocess.TimeoutExpired:
        return f"shell_exec timed out after {_SHELL_STATE['timeout_s']}s"
    except FileNotFoundError as exc:
        return f"shell_exec failed: command not found: {exc}"

    parts = [f"exit={proc.returncode}"]
    if proc.stdout:
        parts.append(f"stdout:\n{proc.stdout[:4000]}")
    if proc.stderr:
        parts.append(f"stderr:\n{proc.stderr[:2000]}")
    return "\n\n".join(parts)
