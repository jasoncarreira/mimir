"""Tool ports for the deepagents-backed agent.

Three distinct patterns from mimir's earlier tool surface translated to
LangChain @tool. The remaining tools (channeltools, scheduletools,
committools, spawn) follow the same patterns.

Patterns covered:
  1. Filter-args + dependency injection (file_search)
  2. JSONL read with structured-shape return (get_turn / mimir_get_turn)
  3. Subprocess execution within the operator-trust boundary (shell_exec)

Each tool's *function-level* dependencies (Indexer, turns_log_path) are
injected via module-level setter functions, parallel to memory_tool.py's
set_memory_client pattern; ``server.py:build_app`` wires them at startup.
"""
from __future__ import annotations

import json
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
    path_prefix: Optional[str] = None,
    semantic_weight: Optional[float] = None,
    keyword_weight: Optional[float] = None,
    recency_weight: Optional[float] = None,
) -> str:
    """Hybrid semantic + keyword search over memory/ and state/ files.

    Use when you need to find a file by topic, not by exact path. If
    you know the path, call read_file directly instead. Returns up
    to k results with path, score, snippet, description.

    Scoring: ``score = w_sem·cosine + w_kw·bm25 + w_rec·recency``.
    Default weights match production tuning (0.5 / 0.2 / 0.3). Override
    via the weight kwargs to bias a search — e.g. ``recency_weight=0.6``
    when looking for the latest version of something, or
    ``semantic_weight=0.8, keyword_weight=0.0`` for pure paraphrase
    match.

    Args:
        query: Natural-language search query.
        scope: One of ``"memory"``, ``"state"``, or ``"all"`` (default).
        k: Max results to return (1-20, default 5).
        path_prefix: Optional subdir under ``scope`` to anchor results
            (e.g. ``"state/journal"`` or ``"state/research"``). Composes
            with ``scope`` — passing both narrows further. If
            ``path_prefix`` is inconsistent with ``scope`` (e.g.
            ``scope="memory", path_prefix="state/journal"``) the
            result is silently empty — the two filters AND together.
        semantic_weight: Weight on cosine similarity. ``None`` → use
            production default (0.5). Must be non-negative.
        keyword_weight: Weight on BM25 keyword match. ``None`` → 0.2.
            Must be non-negative.
        recency_weight: Weight on file-mtime recency decay. ``None`` →
            0.3. Must be non-negative. Passing all three weights as
            zero is accepted but yields ``score=0`` for every match,
            producing arbitrary candidate-pool ordering.

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
    try:
        results = await indexer.search(
            query, scope=scope_clean, k=k_clean,
            path_prefix=path_prefix,
            semantic_weight=semantic_weight,
            keyword_weight=keyword_weight,
            recency_weight=recency_weight,
        )
    except ValueError as exc:
        # Negative-weight rejection from the indexer surfaces as a
        # readable tool error rather than a generic failure.
        return f"file_search failed: {exc}"
    if not results:
        return "(no matches)"
    payload = [r.to_dict() for r in results]
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ────────────────────────────────────────────────────────────────────
# rebuild_index — mid-turn manual INDEX.md trigger
# ────────────────────────────────────────────────────────────────────

_INDEX_GEN_STATE: dict[str, Any] = {"generator": None}

_VALID_SCOPES = frozenset({"memory", "state", "wiki", "all"})


def set_index_generator(generator: Any) -> None:
    """Inject the IndexGenerator the rebuild_index tool will call.

    The IndexGenerator (``mimir.index.IndexGenerator``) manages the
    human-readable ``memory/INDEX.md``, ``state/INDEX.md``, and
    ``state/wiki/index.md`` files. It is distinct from the search
    Indexer (``mimir.search.Indexer``) injected via ``set_indexer``.

    Called once at server startup by ``server.py:build_app``, the same
    way ``set_indexer`` is called for the search Indexer.
    """
    _INDEX_GEN_STATE["generator"] = generator


@tool
async def rebuild_index(scope: str = "all") -> str:
    """Force-rebuild memory/INDEX.md and/or state/INDEX.md mid-turn.

    The post-turn hook rebuilds all indexes automatically after each
    turn completes. Use this when a multi-step workflow writes a file
    in one step and needs the updated INDEX immediately in a later step
    of the *same* turn — e.g. writing a new wiki concept page and then
    cross-linking it within the same turn.

    Args:
        scope: Which index(es) to rebuild. One of ``"memory"``,
            ``"state"``, ``"wiki"``, or ``"all"`` (default). ``"all"``
            rebuilds all three.

    Returns:
        Confirmation string (``rebuild_index ok: scope=<scope>``), or
        an error string on misconfiguration.
    """
    generator = _INDEX_GEN_STATE["generator"]
    if generator is None:
        return "rebuild_index failed: no IndexGenerator configured"
    scope_clean = (scope or "all").strip().lower()
    if scope_clean not in _VALID_SCOPES:
        return (
            f"rebuild_index failed: scope must be one of "
            f"memory/state/wiki/all (got {scope_clean!r})"
        )
    generator.mark_dirty(scope_clean)
    try:
        await generator.flush()
    except Exception as exc:  # noqa: BLE001
        return f"rebuild_index failed during flush: {type(exc).__name__}: {exc}"
    return f"rebuild_index ok: scope={scope_clean}"


# ────────────────────────────────────────────────────────────────────
# Pattern 2: mimir_get_turn — JSONL read, structured return
# ────────────────────────────────────────────────────────────────────

_TURN_STATE: dict[str, Optional[Path]] = {"turns_log_path": None}


def set_turns_log_path(path: Path) -> None:
    """Inject the path to turns.jsonl."""
    _TURN_STATE["turns_log_path"] = path


def _read_turn_record(turn_id: str) -> str:
    """Implementation shared between ``mimir_get_turn`` and its
    ``get_turn`` alias. Reads turns.jsonl, returns the matching record
    JSON-formatted with large derived fields stripped, or an error
    string."""
    if not turn_id or not turn_id.strip():
        return "get_turn failed: turn_id is required"
    path = _TURN_STATE["turns_log_path"]
    if path is None:
        return "get_turn failed: turns log path not configured"
    if not path.exists():
        return f"get_turn failed: turns log not found at {path}"
    target = turn_id.strip()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("turn_id") == target:
                for k in ("input", "saga_atom_ids", "usage"):
                    row.pop(k, None)
                return json.dumps(row, indent=2, ensure_ascii=False)
    return f"get_turn: no turn found with id {target!r}"


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
    return _read_turn_record(turn_id)


@tool
def get_turn(turn_id: str) -> str:
    """Alias of ``mimir_get_turn`` for back-compat with skill prompts.

    Pre-181-N the tool was named ``get_turn`` on main; the deepagents
    cutover renamed it to ``mimir_get_turn`` without leaving a shim.
    Skill markdowns + system prompts that reference the old name would
    silently fail with "tool not found." Both names work now; prefer
    ``mimir_get_turn`` going forward (it's namespaced to mimir).
    """
    return _read_turn_record(turn_id)


# ────────────────────────────────────────────────────────────────────
# Pattern 3: shell_exec — subprocess execution within the trust boundary
# ────────────────────────────────────────────────────────────────────
#
# Trust model (chainlink #226): mimir runs inside an operator-trusted
# container. Both ``shell_exec`` (sync) and ``bash_async`` (long-running)
# can execute arbitrary commands; the agent is trusted with shell access
# the same way the operator who launched the container is. There is no
# in-process allowlist gate — operator-side controls (container
# isolation, capability drops, filesystem mounts) define the boundary.
#
# A previous ``set_shell_allowlist`` affordance existed but was never
# wired in production and gave a misleading appearance of defence; it
# was removed in chainlink #226. If you need to restrict shell access,
# do it at the container layer, or gate ``bash_async`` and ``shell_exec``
# together — half-gating only the sync path is security theatre.

_SHELL_STATE: dict[str, Any] = {
    "cwd": None,
    "timeout_s": 60.0,
}


@tool
def shell_exec(command: str) -> str:
    """Execute a shell command and return stdout + stderr + exit code.

    Runs the command through ``bash -lc`` (a real login shell), so shell
    syntax works: ``cd``-chains, pipes, redirects, ``&&`` / ``||``, globs,
    and environment expansion. This matches ``bash_async`` and the #226
    trust model — the agent is trusted with shell access within the
    operator-configured container, which (not an in-process parse) is the
    security boundary; the prohibited-action guard middleware still screens
    the command string before it runs. Use ``bash_async`` for jobs that may
    exceed the sync timeout.

    Previously this used ``shlex.split`` + ``shell=False`` as an "injection
    guard," but the module's own trust model contradicted it: ``bash_async``
    already exposes a full shell, so half-gating only the sync path was
    security theatre — while it silently broke ``cd``/pipes/redirects/``$``
    for the agent (shell-wrapper fix, 2026-06).

    Args:
        command: The full shell command line (run via ``bash -lc``).

    Returns:
        Formatted block: stdout, stderr, exit code.
    """
    if not command or not command.strip():
        return "shell_exec failed: command is required"
    try:
        proc = subprocess.run(  # noqa: S603 — shell exec by design; trusted container (#226), guard middleware screens the command
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=_SHELL_STATE["timeout_s"],
            cwd=_SHELL_STATE["cwd"],
        )
    except subprocess.TimeoutExpired:
        return f"shell_exec timed out after {_SHELL_STATE['timeout_s']}s"
    except FileNotFoundError as exc:
        return f"shell_exec failed: bash not found: {exc}"

    parts = [f"exit={proc.returncode}"]
    if proc.stdout:
        parts.append(f"stdout:\n{proc.stdout[:4000]}")
    if proc.stderr:
        parts.append(f"stderr:\n{proc.stderr[:2000]}")
    return "\n\n".join(parts)
