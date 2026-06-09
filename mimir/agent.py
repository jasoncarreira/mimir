"""mimir.Agent — deepagents-backed.

Post-cutover (2026-05-14): replaces the 2459-LOC SDK-backed
Agent class with a thin wrapper around LangGraph's deepagents.

Public API preserved:
  - Agent(config, turn_logger, message_buffer, ..., dispatcher=)
  - agent.run_turn(event) -> TurnRecord
  - dispatcher.set_run_turn(agent.run_turn)

What's gone:
  - ClientPool + _PoolEntry + _AcquireContext (~370 LOC) —
    CompiledStateGraph is thread-safe; one shared singleton.
  - SDK message-type handling — turn_logger walks LangChain
    messages instead.
  - HookMatcher chains — replaced by the external wrapper
    pattern (memory pre-inject + post-message credit pass).
  - InMemorySessionStore.delete() per turn — LangGraph handles
    per-call state isolation via the ``thread_id`` config key.
  - claude_agent_sdk dependency at the import level.

What's kept:
  - mimir.SessionManager / SubagentInbox / ChannelRegistry /
    Dispatcher constructor wiring (runtime-agnostic infrastructure).
  - TurnRecord schema (mimir/models.py).
  - The Agent class shape so server.py + tests don't need
    constructor-call rewrites.

Subagent ``task`` tool: deepagents has one built-in. mimir's
spawn-claude-code is a separate runtime concern (subprocess spawn);
currently stubbed — re-wire in a follow-up.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from .channel_registry import ChannelRegistry, is_interactive_turn
from .config import Config
from .event_logger import log_event, log_event_sync, safe_log_event
from .feedback import FeedbackLog
from . import health
from . import mid_turn_injection
from .history import Message, MessageBuffer
from .index import IndexGenerator
from . import _langchain_claude_code_patches as _lcc_patches
from ._jsonl_tail import tail_jsonl_records
from .jsonl_snapshot import JsonlSnapshot
from .models import AgentEvent, TurnRecord
from .prompts import build_system_prompt, build_turn_prompt
from .rate_limits import RateLimitStore
from .saga_client import SagaClient
from .session_boundary_log import (
    count_turns_since,
    render_session_summaries,
)
from .subagent_inbox import SubagentInbox, render_subagent_updates
from .templates import render_saga_session_end
from .usage_stats import event_recently_emitted

# Idempotent runtime patch for langchain-claude-code's ``_arun`` call —
# see the module docstring for the bug + upstream PR. No-op if the
# claude-code extra isn't installed.
_lcc_patches.apply_patches()
# Empty out deepagents' BASE_AGENT_PROMPT so it isn't appended to
# mimir's system prompt. Mimir's prompt is the complete contract
# (persona + memory layers + conventions + skills); the deepagents
# generic framing competes with it. Match the SDK-era invariant of
# "mimir's system_prompt is the only one." No-op when deepagents
# isn't installed.
_lcc_patches.strip_deepagents_base_prompt()
# Preserve SDK ``ResultMessage`` fields (``stop_reason``, ``num_turns``,
# ``is_error``) that langchain-claude-code's streaming wrapper drops —
# without this patch ``derive_result_fields`` loses granular stop-reason
# semantics ("max_turns" / "max_tokens" collapse to binary "stop"/"error")
# and has to approximate ``num_turns`` via ``count(AIMessage)``. Wraps
# ``_astream`` to copy the missing fields from the instance's
# ``_last_result`` (which the upstream code does store) into the result
# chunk's ``generation_info``. No-op when claude-code extra not installed.
_lcc_patches.enrich_streaming_metadata()
# Register PreToolUse/PostToolUse/PostToolUseFailure SDK hooks so every
# tool invocation (built-in Bash/Read/Edit/etc, bridged langchain tools,
# MCP tools) is captured into ``generation_info["tool_events"]`` as an
# ordered list paired by ``tool_use_id``. Without this patch, built-in
# tools never surface results, and bridged-tool calls/results can't be
# paired (call uses the prefixed name, result the bare name). No-op
# when claude-code extra not installed.
_lcc_patches.install_tool_event_hooks()
from .sagatools import (
    _atom_ids_from_response,
    _format_saga_payload,
    _source_atom_ids_from_triples,
)
from .search import Indexer
from .session_manager import SessionManager
from .turn_hooks import TurnHook, fire_hooks
from .turn_logger import (
    TurnLogger,
    derive_result_fields,
    extract_turn_events,
    make_turn_id,
    truncate_input,
)

log = logging.getLogger(__name__)

# Triggers for which saga.query() adds no value — the turn has no
# meaningful user-authored query anchor, so the memory-inject pass
# is skipped entirely. Extraction as a frozenset constant keeps the
# condition readable and easy to extend.
NON_USER_QUERY_TRIGGERS: frozenset[str] = frozenset(
    {"saga_session_end", "scheduled_tick", "poller", "upgrade"}
)

# Autonomous-work triggers — cron-fired and poller-fired turns that
# don't anchor a back-and-forth conversation. After one of these turns
# completes, force-end the saga session immediately instead of letting
# the standard idle-minutes countdown run: the channel won't see more
# turns on the same session anyway (the next cron fire / poller batch
# creates its own session via ``SessionManager.touch``), so a 10-minute
# idle wait just defers the synthesis with no recall benefit.
# ``saga_session_end`` is excluded — that IS the synthesis turn; ending
# the session that just produced it would loop.
IMMEDIATE_SESSION_END_TRIGGERS: frozenset[str] = frozenset(
    {"scheduled_tick", "poller", "upgrade"}
)


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _filter_session_turns(
    turns_path: Path,
    saga_session_id: str,
    *,
    idle_minutes: int = 10,
) -> list[dict]:
    """Read turns.jsonl tail-first and return records with the given
    saga_session_id, in chronological order.

    Time-based break: saga ends a session after ``idle_minutes`` of no
    activity, so any record older than ``newest_match_ts - 2 *
    idle_minutes`` cannot belong to this session. The 2× margin
    tolerates clock skew + a single out-of-order record at the
    boundary. Walks back at most ``2 * idle_minutes`` worth of file
    activity past the last match — O(session_window) rather than
    O(file_size). (Ported verbatim from main.)
    """
    if not turns_path.is_file():
        return []
    margin_seconds = 2 * idle_minutes * 60
    out: list[dict] = []
    newest_match_ts: datetime | None = None
    try:
        for rec in tail_jsonl_records(turns_path):
            if rec.get("saga_session_id") == saga_session_id:
                out.append(rec)
                ts_str = rec.get("timestamp")
                if isinstance(ts_str, str):
                    try:
                        rec_ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if newest_match_ts is None or rec_ts > newest_match_ts:
                            newest_match_ts = rec_ts
                    except ValueError:
                        pass
            elif newest_match_ts is not None:
                ts_str = rec.get("timestamp")
                if isinstance(ts_str, str):
                    try:
                        rec_ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if (newest_match_ts - rec_ts).total_seconds() > margin_seconds:
                            break
                    except ValueError:
                        # Malformed ts on a non-match — keep scanning;
                        # don't break on a record we can't reason about.
                        pass
    except OSError:
        return []
    out.reverse()  # tail yields newest-first; restore chronological
    return out


# ────────────────────────────────────────────────────────────────────
# Model / tool resolution helpers
# ────────────────────────────────────────────────────────────────────


_PROVIDER_EXTRAS: dict[str, str] = {
    # ``claude-code`` is intentionally absent: ``langchain-claude-code``
    # is a git-pinned fork (PyPI rejects packages with direct URL deps),
    # so it's installed as a separate step, not via an extra. See the
    # ImportError message in the claude-code provider branch below for
    # the install incantation. Tracked: issue #268 — restore the extra
    # when upstream publishes a release.
    "anthropic": "anthropic",
    "openai": "openai",
    "codex-plus": "codex-plus",
}


def _supports_responses_api() -> bool:
    """Heuristic for whether to flip ``use_responses_api=True`` on OpenAI.

    Real OpenAI implements the Responses API (``POST /responses``); drop-in
    proxies (Groq, Together, DeepSeek, GLM, …) usually only implement
    ``/chat/completions``, so defaulting to Responses would 404 every turn.
    True when ``OPENAI_BASE_URL`` is unset or its parsed hostname equals
    ``api.openai.com``. ``MIMIR_USE_RESPONSES_API=1|0`` overrides.

    Uses ``urlparse(...).hostname`` rather than substring containment so
    a crafted env value like ``https://api.openai.com.evil.example/v1``
    doesn't trip the flag — the hostname comparison is exact.
    """
    from urllib.parse import urlparse as _urlparse

    override = os.environ.get("MIMIR_USE_RESPONSES_API", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    if not base_url:
        return True
    parsed_host = (_urlparse(base_url).hostname or "").lower()
    return parsed_host == "api.openai.com"


# Reasoning-effort levels each provider accepts, for fail-fast validation.
# Mirrors what the provider packages enforce internally (codex's
# VALID_REASONING_EFFORTS frozenset, langchain-anthropic / claude-agent-sdk
# Literals, OpenAI's gpt-5 levels) so a bad MIMIR_MODEL_REASONING_EFFORT is
# caught here at model construction — with the valid set named — rather than
# erroring deep in a provider call on the first turn. NOTE "none" is
# Codex-only (the others have no "no-reasoning" level).
_EFFORT_LEVELS: dict[str, frozenset[str]] = {
    "codex-plus": frozenset({"none", "low", "medium", "high", "xhigh"}),
    "openai": frozenset({"minimal", "low", "medium", "high"}),
    "anthropic": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "claude-code": frozenset({"low", "medium", "high", "max"}),
}


def _validate_effort(provider: str, effort: str) -> str:
    """Return ``effort`` unchanged if valid for ``provider``, else raise
    ValueError naming the accepted set. Unknown providers pass through."""
    valid = _EFFORT_LEVELS.get(provider)
    if valid is not None and effort not in valid:
        raise ValueError(
            f"MIMIR_MODEL_REASONING_EFFORT={effort!r} is not a valid reasoning "
            f"effort for provider {provider!r}; choose one of {sorted(valid)}"
        )
    return effort


def _resolve_model(
    spec: str | BaseChatModel,
    *,
    max_retries: int = 6,
    max_tokens: int = 0,
    reasoning_effort: str = "",
    rate_limit_callback: Callable[[Any], None] | None = None,
) -> BaseChatModel:
    """Translate a mimir-friendly model spec into a constructed BaseChatModel.

    Supported:
      - ``claude-code:<model>`` → ChatClaudeCode (Max OAuth subprocess)
      - ``codex-plus:<model>`` → ChatCodexPlus (ChatGPT-account
                                  subscription protocol via
                                  ``chatgpt.com/backend-api/codex/responses``)
      - ``<provider>:<model>``  → init_chat_model with ``max_retries`` (and,
                                  for OpenAI hitting api.openai.com,
                                  ``use_responses_api=True``; and ``max_tokens``
                                  when a non-zero output cap is configured)
      - BaseChatModel instance  → pass-through (Bedrock/Vertex/custom)

    ``max_retries`` / ``max_tokens`` only apply to the non-subprocess paths.
    ``reasoning_effort`` is forwarded to every provider that supports it —
    Codex Plus (default ``"none"``), OpenAI, real Claude (``anthropic:`` +
    ``claude-code:``) — and validated against that provider's accepted set
    (``_EFFORT_LEVELS``). Minimax / Kimi (anthropic-compat) are excluded.

    ``rate_limit_callback`` is currently honored only for
    ``codex-plus:``. Pass it from the agent so successful Codex Plus
    responses transcribe their ``x-codex-*`` headers into the
    ``RateLimitStore`` keys that :class:`OpenAIQuotaProvider` reads.

    The model-provider package (``langchain-claude-code``,
    ``langchain-anthropic``, ``langchain-codex-plus``, etc.) is a pip
    extra (see pyproject.toml's ``[project.optional-dependencies]``).
    We lazy-import here so installing only the extras you'll use keeps
    the dep graph small — raising a clear hint on ImportError tells
    the operator exactly which extra they're missing.
    """
    if isinstance(spec, BaseChatModel):
        return spec
    if not isinstance(spec, str):
        raise TypeError(f"unexpected model spec type: {type(spec).__name__}")
    if spec.startswith("claude-code:"):
        try:
            from langchain_claude_code import ChatClaudeCode  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "MIMIR_MODEL_SPEC=claude-code:* requires the "
                "``langchain-claude-code`` package. PyPI rejects "
                "packages with direct URL deps, so it isn't a "
                "mimir-agent extra — install it directly:\n"
                "  pip install \"langchain-claude-code @ git+"
                "https://github.com/jasoncarreira/langchain-claude-code"
                "@c723d702dfac1ff6e2b22b8bde661cb17a17b0de\"\n"
                "Restored as an extra once upstream patches "
                "(see issue #268) merge + a release is cut."
            ) from exc
        model_name = spec.split(":", 1)[1]
        # ``permission_mode="bypassPermissions"`` matches SDK-era
        # mimir's ClaudeAgentOptions setting — without it the claude
        # CLI subprocess gates Write/Bash on user approval and the
        # agent reports "the Write tool is pending approval" instead
        # of actually writing. There's no human in the loop for any
        # mimir deployment (bench / production mimirbot / future
        # daemons), so the approval gate is pure friction. Match the
        # SDK invariant.
        # Thread reasoning effort when set to a real level; "none"/unset →
        # omit it so Claude keeps its adaptive default. Needs the
        # langchain-claude-code fork's ``effort`` field (re-pinned in
        # pyproject) — only passed when non-empty so older builds don't choke.
        cc_kwargs: dict[str, Any] = {
            "model": model_name,
            "permission_mode": "bypassPermissions",
        }
        _cc_eff = (reasoning_effort or "").strip()
        if _cc_eff and _cc_eff != "none":
            cc_kwargs["effort"] = _validate_effort("claude-code", _cc_eff)
        return ChatClaudeCode(**cc_kwargs)
    if spec.startswith("codex-plus:"):
        try:
            from langchain_codex_plus import ChatCodexPlus  # type: ignore[import-untyped]
        except ImportError as exc:
            # Source the extra name from the registry (chainlink #292) so the
            # hint stays correct as the table evolves; also the package is
            # ``mimir-agent``, not ``mimir`` (the old hint named it wrong).
            from .providers import extra_for_spec
            extra = extra_for_spec(spec) or "codex-plus"
            raise ImportError(
                f"MIMIR_MODEL_SPEC=codex-plus:* requires the '{extra}' extra. "
                f"Install via `pip install 'mimir-agent[{extra}]'` "
                "(or `uv pip install langchain-codex-plus`)."
            ) from exc
        from ._langchain_codex_plus_patches import (
            install_codex_plus_transient_retry_patch,
        )
        install_codex_plus_transient_retry_patch(ChatCodexPlus)
        model_name = spec.split(":", 1)[1]
        # reasoning_effort defaults to "none" (mimir's cheap-inference
        # baseline) but is settable across providers via
        # MIMIR_MODEL_REASONING_EFFORT (config.model_reasoning_effort),
        # threaded in here. An empty value keeps the "none" default.
        return ChatCodexPlus(
            model=model_name,
            reasoning_effort=_validate_effort("codex-plus", reasoning_effort or "none"),
            rate_limit_callback=rate_limit_callback,
        )
    # langchain ``init_chat_model`` resolves provider extras at call time
    # (``anthropic:`` → langchain-anthropic, ``openai:`` → langchain-openai).
    # We wrap it here so we can thread max_retries + the responses-API flag
    # through; if the extra isn't installed, init_chat_model raises with
    # the right hint.
    from langchain.chat_models import init_chat_model
    init_params: dict[str, Any] = {"max_retries": max(0, int(max_retries))}
    # Output token cap. 0 = leave the provider default. Set it (via
    # MIMIR_MODEL_MAX_TOKENS) for thinking-via-Anthropic-compat models
    # (Minimax / Kimi), whose reasoning blocks count against the output
    # budget — a small default gets consumed entirely by thinking and the
    # turn hits ``max_tokens`` mid-reasoning with an empty response.
    if max_tokens and int(max_tokens) > 0:
        init_params["max_tokens"] = int(max_tokens)
    if spec.startswith("openai:") and _supports_responses_api():
        init_params["use_responses_api"] = True
    # Reasoning effort, per provider. Skip when unset or "none" (only Codex
    # has a "none" level). OpenAI reasoning models take ``reasoning_effort``;
    # real Claude (langchain-anthropic) takes ``effort``. Gate the Anthropic
    # case on a claude-named spec so Minimax / Kimi on the anthropic-compat
    # endpoint — which don't support effort — are left untouched.
    _effort = (reasoning_effort or "").strip()
    if _effort and _effort != "none":
        if spec.startswith("openai:"):
            init_params["reasoning_effort"] = _validate_effort("openai", _effort)
        elif spec.startswith("anthropic:") and "claude" in spec.lower():
            init_params["effort"] = _validate_effort("anthropic", _effort)
    return init_chat_model(spec, **init_params)


# ────────────────────────────────────────────────────────────────────
# Memory injection (pre-message hook equivalent)
# ────────────────────────────────────────────────────────────────────


# Match atom-id-shaped strings in tool result text (16-char hex).
_ATOM_ID_RE = re.compile(r"\b[0-9a-f]{16}\b")


def _extract_atom_ids_from_tool_results(messages: list[Any]) -> list[str]:
    from langchain_core.messages import ToolMessage
    found: set[str] = set()
    out: list[str] = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            for match in _ATOM_ID_RE.findall(content):
                if match not in found:
                    found.add(match)
                    out.append(match)
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Window for the saga contextual-rewrite context. Matches
# ``mimir.saga.query_rewrite._MAX_CONTEXT_MESSAGES`` so the rewriter's
# last-10-msgs trim doesn't waste effort on entries we'd ship and
# then chop.
_REWRITE_CONTEXT_MESSAGES = 10


def _rewrite_context_from_buffer(
    buffer: MessageBuffer, channel_id: str,
) -> list[dict[str, str]] | None:
    """Render the channel's recent transcript in the shape SagaStore's
    contextual rewrite expects: ``[{role, content}, …]`` with role in
    ``{"user", "assistant"}``.

    ``system_note`` kinds and any other non-conversational records are
    dropped — the rewrite needs reference antecedents, not algedonic
    signals. Returns ``None`` when the channel has no recent
    conversational history so SagaStore short-circuits the LLM call.
    """
    recent = buffer.recent_for_channel(
        channel_id, limit=_REWRITE_CONTEXT_MESSAGES,
    )
    out: list[dict[str, str]] = []
    for msg in recent:
        if msg.kind == "user_message":
            role = "user"
        elif msg.kind == "assistant_message":
            role = "assistant"
        else:
            continue  # skip system_note
        content = (msg.content or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out or None


# Default system prompt — production cutover replaces this with mimir's
def _turn_outcome_identity(event: AgentEvent) -> dict:
    """Origin-correlation fields stamped onto the turn-outcome events
    (``turn_failed`` / ``turn_completed``) so a turn's outcome can be
    traced back to its triggering event — and, for poller turns, to the
    specific item(s) the turn was meant to process (chainlink #262).

    The framework consumer (``mimir.pollers``) reads these off
    events.jsonl to gate a per-poller watermark: advance past items
    whose turn succeeded, re-emit (capped) items whose turn failed —
    closing the "poll advanced the cursor but the triggered turn died"
    drop (#299) for pollers that have no live state to reconcile against
    (gmail, issue comments), the way #516 did for review requests via
    ``requested_reviewers``.

    Returns ``source_id`` for any event that has one (the poller batch's
    stable per-fire id); adds ``poller_name`` + ``items`` (the per-item
    extras list) only for poller-triggered turns, so non-poller turns
    don't bloat their outcome events with absent fields.
    """
    out: dict = {}
    if event.source_id:
        out["source_id"] = event.source_id
    if event.trigger == "poller":
        extra = event.extra or {}
        poller_name = extra.get("poller_name")
        if poller_name:
            out["poller_name"] = poller_name
        items = extra.get("items")
        if items is not None:
            out["items"] = items
    return out


def _count_expected_tool_calls(events: list, markers: dict) -> int:
    """How many ``tool_call`` events satisfy ``markers`` — i.e. how many
    submissions the turn actually made.

    ``markers`` is a dict declared by the event source (e.g. a poller
    putting an entry in its emitted JSONL's ``expected_tool_call``
    field). Shape:

      {
        "tool_names":      ["pull_request_review_write", ...],   # exact name match
        "bash_substrings": ["gh pr review "],                      # substr in shell command
        "signal_on_missing": "poller_review_missed_submission",     # event_type to emit
      }

    Matches an exact tool name (MCP path) OR a shell command containing a
    declared substring. The shell tool is ``shell_exec`` (deepagents);
    ``Bash`` is also matched for the legacy claude-code runtime — pre-fix
    only ``Bash`` was checked, so a ``gh pr review`` run via deepagents'
    shell was invisible to this check (chainlink #299 follow-up).

    Conservative on substring match: a passing reference to a declared
    substring in some other context (echo, sed, grep) would over-count.
    The cost asymmetry (missed signal vs over-counted success) favors
    over-counting.
    """
    if not isinstance(markers, dict):
        return 0
    tool_names = set(markers.get("tool_names") or [])
    bash_substrings = list(markers.get("bash_substrings") or [])
    if not tool_names and not bash_substrings:
        return 0
    count = 0
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("type") != "tool_call":
            continue
        name = ev.get("name") or ""
        if name and name in tool_names:
            count += 1
            continue
        if name in ("shell_exec", "Bash") and bash_substrings:
            args = ev.get("args") or {}
            command = args.get("command") if isinstance(args, dict) else ""
            if isinstance(command, str) and any(s in command for s in bash_substrings):
                count += 1
    return count


def _turn_matched_expected_tool_call(events: list, markers: dict) -> bool:
    """Back-compat boolean: did the turn make at least one matching
    submission? (Equivalent to ``_count_expected_tool_calls(...) > 0``.)"""
    return _count_expected_tool_calls(events, markers) > 0


def _expected_submission_markers(event_extra: dict) -> list[dict]:
    """The expected-tool-call markers for an event — one per review-needed
    item.

    Per-item markers under ``extra["items"]`` (poller batch events wrap
    per-item metadata there) are authoritative: when present they ARE the
    set. A top-level ``expected_tool_call`` is consulted ONLY when there are
    no per-item markers (a direct, non-batch event). Including both would
    double-count ``expected`` — the top-level marker on a batch is the
    shared declaration, not an extra Nth item (chainlink #308 / finding
    #38).

    The per-item lookup itself was chainlink #299's follow-up: every poller
    AgentEvent is batch-wrapped (``extra={"items": [...]}``), so a
    top-level-only read found no marker and the missed-submission check
    never ran for poller reviews — letting "drafted but never submitted"
    reviews pass silently (observed: a turn claimed it approved PR #522 on
    GitHub but never called ``gh pr review``)."""
    if not isinstance(event_extra, dict):
        return []
    item_markers = [
        item["expected_tool_call"]
        for item in (event_extra.get("items") or [])
        if isinstance(item, dict) and isinstance(item.get("expected_tool_call"), dict)
    ]
    if item_markers:
        return item_markers
    top = event_extra.get("expected_tool_call")
    return [top] if isinstance(top, dict) else []


# full system-prompt assembly (core memory + skills + persona).
_DEFAULT_SYSTEM_PROMPT = """\
You are a memory-augmented assistant. The user is asking about facts \
from their past conversations.

Use the ``memory_query`` tool to search the user's persistent memory. \
The tool returns observations (synthesized beliefs), evidence (raw \
chat history with dates — prefer for specifics), and triples \
(structured facts with valid-date ranges).

When the memory tool's result is truncated or incomplete, call \
``memory_query`` again with a more specific query.

Think step by step:
1. Which atoms / triples answer the question?
2. If multiple conflict, which is most recent?
3. If no evidence answers, say so plainly.

Then give the final answer on its own line. Be concise."""


# ────────────────────────────────────────────────────────────────────
# Agent class — public API surface preserved
# ────────────────────────────────────────────────────────────────────


class Agent:
    """Deepagents-backed mimir Agent.

    Constructor signature matches the legacy SDK-backed Agent so
    server.py + tests don't need rewrites at the call site. Internally
    builds a deepagents CompiledStateGraph singleton and dispatches
    each event through ``run_turn``.
    """

    def __init__(
        self,
        config: Config,
        turn_logger: TurnLogger,
        message_buffer: MessageBuffer,
        index_generator: IndexGenerator,
        indexer: Indexer | None = None,
        saga_client: SagaClient | None = None,
        session_manager: SessionManager | None = None,
        scheduler: Any = None,
        subagent_inbox: SubagentInbox | None = None,
        channel_registry: ChannelRegistry | None = None,
        dispatcher: Any = None,
        commitments_store: Any = None,
        turn_hooks: list[Any] | None = None,
    ) -> None:
        self._config = config
        self._turn_logger = turn_logger
        self._buffer = message_buffer
        self._indexes = index_generator
        self._indexer = indexer
        self._saga = saga_client
        # chainlink #266: concrete SagaStore handle (peeled from the
        # saga_client wrapper chain in _try_inject_memory_client), used by
        # the skill-memory load injection. None until/unless a SagaStore
        # is found.
        self._saga_store: Any = None
        self._sessions = session_manager
        self._scheduler = scheduler
        self._inbox = subagent_inbox or SubagentInbox()
        self._channels = channel_registry
        self._dispatcher = dispatcher
        # Phase 2b due-check poller (server.py:_on_startup) reads this
        # attribute via getattr. Pre-fix it was always None — every
        # commitment_complete / _snooze / _dismiss / _list tool call
        # also returned "no commitments store" because the registry
        # setter was never invoked. Both paths now wired by build_app.
        self._commitments = commitments_store
        # Turn lifecycle hooks (re-introduced in PR #213 after the
        # SDK-era chain was dropped in #181). Fired by
        # ``mimir.turn_hooks.fire_hooks(stage, self._hooks, ...)``
        # with per-hook exception isolation; failures emit a
        # ``turn_hook_failed`` event. When the caller passes
        # ``turn_hooks=None``, the agent installs the default
        # commitments-extraction hook so single-agent deployments
        # get the same finalize behavior they had pre-refactor.
        # Pass ``turn_hooks=[]`` to opt out entirely (test paths).
        # Use ``Agent.add_hook(...)`` to append after construction.
        if turn_hooks is None:
            from .turn_hooks import CommitmentExtractionHook
            self._hooks: list[TurnHook] = [
                CommitmentExtractionHook(commitments_store),
            ]
        else:
            self._hooks = list(turn_hooks)

        # CR#10: per-Agent JsonlSnapshot caches for events.jsonl +
        # turns.jsonl. Six per-turn call sites (feedback, usage, self-
        # state, session summaries, subagent aggregate, budget
        # partition) used to re-stream both files each time. The
        # snapshot wraps tail_jsonl_records with an mtime-checked TTL so
        # within one turn (~1s wall-clock) all those readers share one
        # cached parse.
        self._events_snapshot = JsonlSnapshot(config.events_log)
        self._turns_snapshot = JsonlSnapshot(config.turns_log)

        # Recent feedback signals block — algedonic surface for the
        # turn prompt (v0.4 §2).
        self._feedback = FeedbackLog(
            events_path=config.events_log,
            turns_path=config.turns_log,
            default_window_hours=config.feedback_window_hours,
            default_limit_per_polarity=config.feedback_limit_per_polarity,
            events_snapshot=self._events_snapshot,
            turns_snapshot=self._turns_snapshot,
            resolved_incidents_path=config.home / "resolved-incidents.jsonl",
        )
        # Plan-window rate-limit state — real RateLimitStore (replaces
        # the deprecated _RateLimitStub). The oauth_usage_poller writes
        # to this same JSON file from server.py's wiring; per-turn
        # readers (usage block, self-state, upcoming) read through here.
        self._rate_limits = RateLimitStore(
            path=config.home / ".mimir" / "rate_limits.json",
        )

        # §12.4: S3-S4 homeostat. Constructed once so scheduler heart-
        # beats and per-turn ``## Self-state`` render share the same
        # instance. Wire into the scheduler immediately so heartbeats
        # fired before the first turn are still arbitrated.
        from .billing import build_quota_providers
        from .budget import HomeostaticArbiter
        # Auto-discover quota providers based on routing config.
        # Discovery is layered: ``MIMIR_MODEL_SPEC`` prefix wins
        # (``openai:`` / ``claude-code:`` are explicit provider picks),
        # then ``ANTHROPIC_BASE_URL`` host disambiguates ``anthropic:*``
        # routes (Minimax compat vs canonical Anthropic).
        quota_providers = build_quota_providers(
            store=self._rate_limits,
            billing_mode=config.billing_mode,
            model_spec=config.model_spec,
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", ""),
        )
        self._arbiter = HomeostaticArbiter(
            home=config.home,
            rate_limit_store=self._rate_limits,
            turns_log=config.turns_log,
            billing_mode=config.billing_mode,
            quota_providers=quota_providers,
            cost_hourly_limit_usd=config.cost_hourly_limit_usd or None,
            cost_spike_ratio=config.cost_rate_spike_ratio or None,
            cost_spike_floor_usd=config.cost_rate_spike_floor_usd or None,
            fallback_model=config.model,
            events_snapshot=self._events_snapshot,
            turns_snapshot=self._turns_snapshot,
        )
        if scheduler is not None:
            scheduler._arbiter = self._arbiter

        # Bounded set for fire-and-forget background tasks. Without
        # retaining a reference, the asyncio task can be GC'd before
        # the coroutine body runs. add+discard idiom from PEP 458 /
        # asyncio docs.
        self._bg_tasks: set[asyncio.Task] = set()

        # Async-shell job registry — backs the bash_async /
        # bash_jobs_list / bash_job_output tools. One registry per
        # Agent (process-scoped); waiter threads spawned by
        # ``spawn()`` live for the duration of the subprocess they
        # wrap. Files land in ``<home>/logs/bash-jobs/<job_id>.{out,err}``.
        # Wired into the tool surface from server.py:build_app via
        # ``mimir.tools.set_shell_job_registry(...)``.
        from .shell_jobs import ShellJobRegistry as _ShellJobRegistry
        self._shell_jobs = _ShellJobRegistry(
            jobs_dir=config.home / "logs" / "bash-jobs",
        )

        # Captured at first turn (when we know we're on the asyncio
        # loop). The shell-job waiter threads use this to schedule
        # the completion handler back onto the loop via
        # ``run_coroutine_threadsafe``.
        self._loop: asyncio.AbstractEventLoop | None = None

        # Build the deepagent singleton. Done lazily to keep import-time
        # fast and to let tests construct Agent without a real model.
        # Lock-guarded against concurrent first turns racing in and
        # constructing two CompiledStateGraphs — pre-fix the second
        # would clobber the first (harmless but wasteful since each
        # graph is heavyweight). asyncio.Lock created lazily because
        # __init__ may run outside a running event loop (tests).
        self._agent: Any | None = None
        self._cached_system_prompt: str | None = None
        self._agent_lock: asyncio.Lock | None = None
        self._backend: Any | None = None
        self._agent_model: Any | None = None
        self._agent_tools: list[Any] | None = None
        self._agent_middleware: tuple[Any, ...] | None = None
        self._cached_skill_catalog_fingerprint: str | None = None

        # Memory-tool dep injection — only used if saga_client is a
        # SagaStore (post-saga cutover). Wires up the @tool's
        # SagaStore handle so deepagents can call into recall.
        if saga_client is not None:
            self._try_inject_memory_client(saga_client)

    def _try_inject_memory_client(self, saga_client: SagaClient) -> None:
        """If saga_client is a SagaStore (or wraps one at any depth),
        wire it into the memory_query / memory_store tools.

        Production saga_client is a RecordingSagaClient wrapping
        either _InProcessSaga (legacy) or SagaStore. Test harnesses
        and bench middleware may add additional wrappers (capture
        proxies, recording layers); we peel ``_inner`` until we find
        a concrete SagaStore or run out of layers.
        """
        try:
            from .saga.client import SagaStore
        except Exception:
            return
        candidate: Any = saga_client
        seen: set[int] = set()
        # Peel ``_inner`` chains — RecordingSagaClient, _MemoryStateProxy,
        # any test/bench wrapper that follows the convention.
        while candidate is not None and id(candidate) not in seen:
            seen.add(id(candidate))
            if isinstance(candidate, SagaStore):
                from .tools import set_memory_client
                set_memory_client(candidate)
                # chainlink #266: stash the concrete SagaStore so the
                # skill-memory load injection can recall skill_learning
                # atoms via its connection. None when no SagaStore is
                # wired (legacy _InProcessSaga / test stubs) — injection
                # then no-ops.
                self._saga_store = candidate
                return
            candidate = getattr(candidate, "_inner", None)

    # ── Conversational buffer (chat_history) append helpers ────────
    # Restored after PR #181 (deepagents migration) — the SDK-era code
    # called ``buffer.append`` inline in the pre/post hooks; the rewrite
    # dropped those calls. As a result ``chat_history.jsonl`` stopped
    # being appended to (last entry 2026-05-17T21:48) and the agent's
    # ``## Recent activity`` block was whatever the buffer's ``replay()``
    # loaded at last process start. Restoring inbound (here) + outbound
    # (at every ``bridge.send`` site) puts the conversation back on
    # disk and in the deques.

    # Triggers that are internal wakes with no conversational content;
    # logging them would pollute Recent activity with system noise
    # the agent doesn't need to re-read. Matches the pre-#181 deny-list
    # in ``_record_inbound``. Everything else — ``user_message``,
    # ``poller``, ``scheduled_tick``, ``react_received``, etc. — IS
    # logged (``user_message`` → kind=user_message, all others →
    # kind=system_note) so the agent's view of "what just happened on
    # this channel" stays accurate.
    _INBOUND_SKIP_TRIGGERS: frozenset[str] = frozenset(
        {"saga_session_end", "shell_job_complete"}
    )

    def add_hook(self, hook: TurnHook) -> None:
        """Register an additional ``TurnHook`` to fire during the
        turn lifecycle. Hooks run in registration order; pass-through
        post-construction registration path for callers that want to
        layer extra behavior on top of the default chain (e.g.
        Muninnbot adding its own ``finalize``-stage knowledge-graph
        sync without rebuilding the constructor's hook list).
        """
        self._hooks.append(hook)

    @staticmethod
    def _kind_for_trigger(trigger: str) -> str:
        """Map an ``AgentEvent.trigger`` to a ``MessageKind``."""
        if trigger == "user_message":
            return "user_message"
        # Pollers (github-activity etc.), scheduled ticks, and other
        # synthetic events are author-less but still conversation-
        # adjacent — agent reads them in Recent activity to understand
        # what woke it up. Pre-#181 logged them as system_note.
        return "system_note"

    async def _append_inbound_to_buffer(self, event: AgentEvent) -> None:
        """Append an inbound event to the ``MessageBuffer`` so future
        ``assemble_recent_activity`` calls see it. Skip rules match
        pre-#181's ``_record_inbound``: drop empty-content events
        and internal-wake triggers (``saga_session_end``,
        ``shell_job_complete``); log everything else.

        Idempotent per event (chainlink #376 PR 4): a mid-turn message is
        recorded here at inject time (via the dispatcher's on_inject hook); if it
        was never folded and re-routes as its own turn, the normal inbound call
        sees the ``_buffer_recorded`` flag and no-ops instead of double-recording.
        """
        if not event.content or not event.channel_id:
            return
        if event.trigger in self._INBOUND_SKIP_TRIGGERS:
            return
        if event.extra.get("_buffer_recorded"):
            return
        try:
            msg = self._buffer.make_message(
                channel_id=event.channel_id,
                kind=self._kind_for_trigger(event.trigger),
                content=event.content,
                author=event.author,
                # Fall back to ``author`` when display is unset so the
                # render layer never has to display a raw platform key.
                author_display=event.author_display or event.author,
                msg_id=event.source_id,
                source=event.source,
            )
            await self._buffer.append(msg)
            event.extra["_buffer_recorded"] = True
        except Exception:  # noqa: BLE001
            log.exception(
                "inbound buffer append failed for event trigger=%r channel=%r",
                event.trigger, event.channel_id,
            )

    async def on_message_injected(self, event: AgentEvent) -> None:
        """Dispatcher ``on_inject`` hook (chainlink #376 PR 4): a mid-turn
        message was just folded into a running turn. Record it in chat history
        now, at its true arrival time, so Recent activity threads it ahead of the
        turn's later replies. Wired in server.py via ``set_on_inject``."""
        await self._append_inbound_to_buffer(event)

    def _current_system_prompt(self, *, emit_health_events: bool = False) -> str:
        """Render the system prompt for the current turn.

        ``create_deep_agent`` bakes ``system_prompt`` into the compiled
        graph, so the graph must be rebuilt when the rendered prompt
        changes. The comparison path suppresses health events; the
        changed/first-build path re-renders with events enabled so
        ``core_prompt_degraded`` fires once per changed prompt rather
        than once per turn.
        """
        override = os.environ.get("MIMIR_SYSTEM_PROMPT_OVERRIDE")
        if override is not None:
            return override
        return self._build_system_prompt(emit_health_events=emit_health_events)

    def _current_skill_sources(self) -> list[str]:
        """Return the skill source directories in deepagents shadowing order."""
        from .skill_defs import (
            home_builtin_skills_dir,
            home_skills_dir,
        )

        skill_sources: list[str] = []
        operator_dir = home_skills_dir(self._config.home)
        builtin_dir = home_builtin_skills_dir(self._config.home)
        # Bundled (read-only intent) listed first so operator entries
        # shadow same-named bundled ones.
        if builtin_dir.is_dir():
            skill_sources.append(str(builtin_dir))
        if operator_dir.is_dir():
            skill_sources.append(str(operator_dir))
        return skill_sources

    def _skill_catalog_fingerprint(self, skill_sources: list[str]) -> str:
        """Fingerprint the discovered skill catalog inputs.

        Deepagents' SkillsMiddleware receives source directories, but the
        middleware may cache the discovered catalog inside the compiled graph.
        Include every ``SKILL.md`` file's path and content hash in the graph
        cache key so adding/removing/editing a skill takes effect on the next
        turn without rebuilding model/tool objects.
        """
        digest = hashlib.sha256()
        for source in skill_sources:
            root = Path(source)
            digest.update(str(root).encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            if not root.is_dir():
                digest.update(b"missing\0")
                continue
            for path in sorted(root.rglob("SKILL.md")):
                try:
                    rel = path.relative_to(root)
                    data = path.read_bytes()
                except OSError:
                    continue
                digest.update(str(rel).encode("utf-8", "surrogateescape"))
                digest.update(b"\0")
                digest.update(hashlib.sha256(data).digest())
                digest.update(b"\0")
        return digest.hexdigest()

    async def _build_agent_if_needed(self) -> Any:
        # ``create_deep_agent`` freezes ``system_prompt`` at graph
        # construction time. Re-render it on every call and use the
        # byte-identical prompt as the cache key: unchanged turns reuse
        # the graph (prompt-cache prefix stays warm), while core-memory /
        # index / operator-config changes take effect on the next turn
        # without a process restart (chainlink #369).
        system_prompt = self._current_system_prompt(emit_health_events=False)
        skill_sources = self._current_skill_sources()
        skill_catalog_fingerprint = self._skill_catalog_fingerprint(skill_sources)
        if self._agent is not None and self._cached_system_prompt is None:
            # Unit tests inject a fake graph directly to avoid constructing
            # deepagents. Production-built graphs always set
            # ``_cached_system_prompt`` below, so this compatibility path
            # does not bypass prompt-change invalidation in live runs.
            return self._agent
        if (
            self._agent is not None
            and system_prompt == self._cached_system_prompt
            and skill_catalog_fingerprint == self._cached_skill_catalog_fingerprint
        ):
            return self._agent

        if self._agent_lock is None:
            self._agent_lock = asyncio.Lock()
        async with self._agent_lock:
            # Re-render under the lock: a contending turn may have just
            # rebuilt the graph for the same prompt while we waited, or
            # the prompt may have changed again on disk.
            system_prompt = self._current_system_prompt(emit_health_events=False)
            skill_sources = self._current_skill_sources()
            skill_catalog_fingerprint = self._skill_catalog_fingerprint(skill_sources)
            if (
                self._agent is not None
                and system_prompt == self._cached_system_prompt
                and skill_catalog_fingerprint == self._cached_skill_catalog_fingerprint
            ):
                return self._agent

            # This is the first build or an actual prompt change. Emit
            # health events for the final rendered prompt only on this
            # path, not on every unchanged-turn comparison.
            if os.environ.get("MIMIR_SYSTEM_PROMPT_OVERRIDE") is None:
                system_prompt = self._current_system_prompt(emit_health_events=True)
                if (
                    self._agent is not None
                    and system_prompt == self._cached_system_prompt
                    and skill_catalog_fingerprint
                    == self._cached_skill_catalog_fingerprint
                ):
                    return self._agent

            from deepagents import create_deep_agent
            from .readonly_backend import WriteGuardBackend
            from .tools import all_mimir_tools

            # Per-directory write-permission enforcement (Config.folders).
            # Read tools (Glob/Grep/Read) stay unrestricted; Write/Edit/upload
            # outside ``writable_dirs`` return a permission error instead of
            # mutating the filesystem. ``.mimir/`` (saga db, metrics) is
            # implicitly blocked because it's not in the folders dict.
            if self._backend is None:
                self._backend = WriteGuardBackend(
                    root_dir=self._config.home,
                    writable_dirs=self._config.writable_dirs,
                )

            # Bridge ChatCodexPlus's per-response x-codex-* headers
            # into the same RateLimitStore that OpenAIQuotaProvider
            # reads (closes the writer-side gap from PR #248). For
            # non-codex-plus specs the callback is unused — the
            # standard ChatOpenAI / Anthropic clients don't expose a
            # rate_limit_callback and the kwarg is ignored.
            if self._agent_model is None:
                from .billing import make_codex_plus_rate_limit_callback
                codex_plus_callback = make_codex_plus_rate_limit_callback(
                    self._rate_limits
                )
                # Config carries the operator-set model spec; env override
                # exists for ad-hoc bench / smoke runs that don't go through
                # Config.from_env. See Config.model_spec for the format
                # (``claude-code:<model>`` or ``<provider>:<model>``).
                model_spec = os.environ.get(
                    "MIMIR_MODEL_SPEC",
                    getattr(
                        self._config,
                        "model_spec",
                        "claude-code:claude-sonnet-4-6",
                    ),
                )
                self._agent_model = _resolve_model(
                    model_spec,
                    max_retries=getattr(self._config, "model_max_retries", 6),
                    max_tokens=getattr(self._config, "model_max_tokens", 0),
                    reasoning_effort=getattr(
                        self._config, "model_reasoning_effort", ""
                    ),
                    rate_limit_callback=codex_plus_callback,
                )

            if self._agent_tools is None:
                self._agent_tools = all_mimir_tools()

            # Skills surfaced via SkillsMiddleware: pass operator +
            # bundled source paths as discovery sources. The framework
            # scans each source for ``<name>/SKILL.md`` entries and
            # renders a catalog into the system prompt. ``skill_sources``
            # is recomputed each turn; its catalog fingerprint above
            # decides whether the graph has to be rebuilt.

            if self._agent_middleware is None:
                # ``BudgetGateMiddleware`` enforces the per-turn tool-call
                # budget at the langchain middleware layer so it catches
                # BOTH mimir-registered tools and deepagents' built-ins
                # (shell_exec, read_file, write_file, glob, edit_file,
                # write_todos). Pre-fix the budget gate wrapped each
                # ``all_mimir_tools()`` entry individually and missed the
                # built-ins — production heartbeats hit 142 tool_calls
                # vs a budget of 120 with zero denials firing.
                from .tools.budget_gate import BudgetGateMiddleware
                # chainlink #266 (slice 3): on non-poller turns the model
                # loads a skill by read_file-ing its SKILL.md; this middleware
                # appends that skill's recorded learnings to the result. Ordered
                # AFTER the budget gate so a budget-denied read never triggers
                # injection (it's order-robust regardless — it only augments
                # successful read_file results on a <skill>/SKILL.md path).
                from .tools.skill_memory_inject import SkillMemoryInjectionMiddleware
                # chainlink #376 (PR 1): folds queued mid-turn user messages into
                # the running turn at each model-call boundary. Ordered LAST so
                # the fold-in is additive and never bypasses the budget gate.
                # Dormant until the dispatcher feeds the queue (PR 2) — a no-op
                # (empty queue) on every turn today.
                from .mid_turn_injection import MidTurnInjectionMiddleware
                self._agent_middleware = (
                    BudgetGateMiddleware(),
                    SkillMemoryInjectionMiddleware(),
                    MidTurnInjectionMiddleware(),
                )

            self._agent = create_deep_agent(
                model=self._agent_model,
                tools=self._agent_tools,
                system_prompt=system_prompt,
                backend=self._backend,
                skills=skill_sources or None,
                middleware=self._agent_middleware,
            )
            self._cached_system_prompt = system_prompt
            self._cached_skill_catalog_fingerprint = skill_catalog_fingerprint
            return self._agent

    async def run_turn(self, event: AgentEvent) -> TurnRecord:
        """Run one agent turn — preserves the SDK Agent.run_turn contract."""
        turn_id = make_turn_id()
        t_total_start = time.monotonic()
        # chainlink #383: arm mid-turn injection before any slow setup work
        # (session touch, SAGA query, prompt/index assembly). The dispatcher
        # already marks the channel in-flight before invoking run_turn; keeping
        # the registry in lockstep closes the setup blind window where a
        # follow-up user_message saw dispatcher._in_flight but inject_message()
        # returned no_active_turn and fell into the FIFO. The model-loop finally
        # still owns deactivate(); setup-phase exceptions explicitly deactivate
        # below so early arming cannot leak a stale active entry.
        injection_registered = False
        if event.channel_id and event.trigger == "user_message":
            mid_turn_injection.register_inflight(event.channel_id)
            injection_registered = True
        # Capture the asyncio loop once so shell-job waiter threads
        # can schedule their completion handlers back onto it via
        # ``asyncio.run_coroutine_threadsafe``.
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        # Session attach — same as the SDK path.
        session_id = event.channel_id or "default"
        saga_session_id: str | None = None
        if event.trigger == "saga_session_end":
            saga_session_id = (event.extra or {}).get("saga_session_id")
        elif self._sessions is not None:
            sess = await self._sessions.touch(event.channel_id)
            saga_session_id = sess.saga_session_id
            self._sessions.increment_turn_count(event.channel_id)

        # Typing indicator: fire at turn START on interactive turns so the
        # user sees "mimir is typing…" the moment their message lands and for
        # the whole turn (0.3.0 removed auto-dispatch, so this is the only
        # received-it signal until the agent calls send_message). It is
        # released in the finally (turn END) — NOT on send_message — so it
        # persists across multi-part replies. Bridges without the method
        # (Bench / WebChat) are silently skipped.
        _typing_bridge = None
        if (
            self._channels is not None
            and event.channel_id
            and is_interactive_turn(event.channel_id, event.trigger, self._channels)
        ):
            _typing_bridge = self._channels.find(event.channel_id)
            if _typing_bridge is not None and hasattr(
                _typing_bridge, "send_typing_indicator"
            ):
                try:
                    await _typing_bridge.send_typing_indicator(event.channel_id)
                except Exception as exc:  # noqa: BLE001
                    log.debug("typing indicator failed: %s", exc)

        # Set up TurnContext so RecordingSagaClient can populate
        # ctx.saga_calls. Without this all saga calls — pre-message
        # query, post-message feedback — were lost from the turn
        # record. The context is reset in the finally block.
        from .models import TurnContext as _TurnContext
        from ._context import set_current_turn, reset_current_turn
        from .loop_detector import LoopDetector
        ctx = _TurnContext(
            turn_id=turn_id,
            session_id=session_id,
            trigger=event.trigger,
            channel_id=event.channel_id,
            started_at=t_total_start,
            agent_id=self._config.agent_id,
            saga_session_id=saga_session_id,
            # Per-turn send-loop circuit breaker (SPEC §7.2.4). Attached
            # to the TurnContext so send_message can reach it via
            # ``_context.get_current_turn()`` without a separate
            # parameter-passing path. Soft/hard limits + similarity
            # threshold come from Config (mimir/config.py default 5/10/0.9).
            # Pre-181-J the detector wasn't constructed at all; the
            # circuit breaker was disarmed and the agent could ship
            # near-duplicate sends indefinitely.
            loop_detector=LoopDetector(
                soft_limit=self._config.send_loop_soft_limit,
                hard_limit=self._config.send_loop_hard_limit,
                similarity_threshold=self._config.send_loop_similarity,
            ),
            # Tool-call budget (181-N). The langchain @tool wrappers
            # installed by ``apply_budget_gate`` in registry.py read
            # this off the TurnContext and refuse tool calls past
            # the cap. 0 disables (matches main's contract).
            tool_call_budget=self._config.tool_call_budget,
            # Source of the inbound channel (e.g. "discord", "slack"),
            # recorded on the turn context for observability. The outbound
            # buffer-append now lives only in the send_message tool, which
            # tags the message with ``bridge.name`` so it passes the
            # recent_sources allowlist in recent_for_channel (chainlink #270);
            # this field is no longer the source of that tag.
            channel_source=event.source,
        )
        # WikiBacklinksHook pre-snapshot — capture mtimes of every
        # state/wiki/ content page BEFORE the model loop runs so the
        # finalize step can tell if any wiki page was edited this turn.
        # Stored on ctx (NOT on self) so concurrent turns on different
        # channels don't share state — multi-channel-correctness
        # invariant from the SDK build. Empty dict when the wiki dir
        # doesn't exist; finalize early-returns in that case.
        ctx.wiki_mtime_snapshot = self._snapshot_wiki_mtimes()

        ctx_token = set_current_turn(ctx)
        # Populate the module-global current_channel_id as a fallback
        # for the claude-code path. ChatClaudeCode dispatches tools
        # via the ClaudeSDKClient subprocess; the SDK round-trips back
        # through ``_langchain_claude_code_patches`` which calls
        # ``tool._arun(**args, config=RunnableConfig())`` — a fresh
        # empty config. The RunnableConfig route added in 181-B
        # therefore can't see ``configurable["channel_id"]`` on that
        # path, and send_message / react / fetch_channel_history would
        # fail with "no channel_id and no current channel" when the
        # model omits the arg. Setting _STATE here closes the gap; the
        # helper still prefers configurable when present, so direct
        # LangGraph tool dispatch (anthropic/openai providers) keeps
        # the race-free route. The dispatcher serializes turns per-
        # channel, so the cross-channel race Mimir originally flagged
        # is constrained to the moment between set and reset here.
        from .tools.registry import (
            set_current_channel_id as _set_cid,
            reset_current_channel_id as _reset_cid,
            set_current_turn_interactive as _set_interactive,
            reset_current_turn_interactive as _reset_interactive,
        )
        cid_token = _set_cid(event.channel_id)
        # Pair the interactivity flag with the channel id: send_message uses it
        # to decide whether a channel-less call may default to this turn's
        # channel (interactive) or must be given an explicit channel (non-
        # interactive heartbeat / poller / synthesis / upgrade turns).
        interactive_token = _set_interactive(
            is_interactive_turn(event.channel_id, event.trigger, self._channels)
        )
        try:
            return await self._run_turn_body(
                event, ctx, ctx_token, turn_id, session_id, saga_session_id,
                t_total_start,
            )
        except Exception as exc:
            # chainlink #306: _run_turn_body's model-loop try/except emits
            # turn_failed/turn_completed only AFTER the early phase (prompt
            # build, agent construction, model resolution). A crash in that
            # early phase would otherwise propagate here with NO terminal
            # outcome logged — and poller-recovery (#262), which keys off
            # turn_failed/turn_completed, would leak the in-flight item
            # forever (never retried). Guarantee a turn_failed for poller
            # turns whose outcome wasn't already emitted, then re-raise so
            # the dispatcher's own error handling is unchanged. Only
            # ``Exception`` (not CancelledError) — a cancelled turn isn't a
            # failure to recover.
            if event.trigger == "poller" and not getattr(
                ctx, "outcome_emitted", False
            ):
                try:
                    await log_event(
                        "turn_failed",
                        channel_id=event.channel_id,
                        turn_id=turn_id,
                        trigger=event.trigger,
                        error=f"{type(exc).__name__}: {exc}"[:240],
                        phase="pre_model_loop",
                        **_turn_outcome_identity(event),
                    )
                except Exception:  # noqa: BLE001 — never mask the original
                    log.exception("early-phase turn_failed emit failed")
            raise
        finally:
            if injection_registered:
                # _run_turn_body normally deactivates once the model loop starts.
                # This covers pre-body cancellations/exceptions that bypass that
                # finally without relying on register_inflight's next-turn overwrite.
                leftover_injections = mid_turn_injection.deactivate(event.channel_id)
                if leftover_injections and self._dispatcher is not None:
                    self._dispatcher.requeue_front(leftover_injections)
            # Release the typing indicator at turn end (held from turn start
            # across any send_message calls). In the finally so it fires on
            # success, error, and cancellation alike.
            if _typing_bridge is not None and hasattr(_typing_bridge, "cancel_typing"):
                try:
                    await _typing_bridge.cancel_typing(event.channel_id)
                except Exception as exc:  # noqa: BLE001
                    log.debug("cancel_typing failed: %s", exc)
            reset_current_turn(ctx_token)
            _reset_interactive(interactive_token)
            _reset_cid(cid_token)

    async def _run_turn_body(
        self,
        event: AgentEvent,
        ctx: Any,
        ctx_token: Any,
        turn_id: str,
        session_id: str,
        saga_session_id: str | None,
        t_total_start: float,
    ) -> TurnRecord:

        # Persist the inbound event to the chat-history buffer + JSONL
        # BEFORE any other turn work so ``assemble_recent_activity``
        # in ``_build_turn_prompt`` sees this turn's own trigger as
        # context. No-op for triggers that don't represent conversation
        # (scheduled_tick / saga_session_end / shell_job_complete).
        await self._append_inbound_to_buffer(event)

        # chainlink #383 facet 1: mop up same-channel user messages that
        # queued behind an interactive user turn before the dispatcher/registry
        # made it injectable. The dispatcher drains only the contiguous
        # user_message prefix, so non-user queued predecessors remain an
        # ordering boundary. Do NOT drain for non-user triggers (session
        # synthesis, react, shell-job, etc.): folding a user's queued message
        # into a non-conversational turn can silently swallow it with no
        # user-facing reply. Enqueue-time injection is likewise limited by
        # only arming the registry for user_message turns at run_turn start.
        if self._dispatcher is not None and event.trigger == "user_message":
            startup_events = self._dispatcher.drain_startup_user_messages(event.channel_id)
            if startup_events:
                accepted = mid_turn_injection.inject_startup_messages(
                    event.channel_id, startup_events,
                )
                if accepted:
                    await log_event(
                        "mid_turn_startup_injected",
                        channel_id=event.channel_id,
                        count=accepted,
                    )
                    for startup_event in startup_events[:accepted]:
                        await self.on_message_injected(startup_event)
                if accepted < len(startup_events) and self._dispatcher is not None:
                    self._dispatcher.requeue_front(startup_events[accepted:])

        # Pre-message memory inject. Builds the "Possibly relevant
        # memories" block + collects atom_ids for the post-turn
        # feedback credit pass. Skipped for triggers in
        # NON_USER_QUERY_TRIGGERS (saga_session_end, scheduled_tick,
        # poller) — these turns have no user-authored query anchor so
        # retrieval is wasteful noise; session summaries still fire
        # via _assemble_session_summaries for all trigger types.
        memory_block: str | None = None
        saga_atom_ids: list[str] = []
        if self._saga is not None and event.trigger not in NON_USER_QUERY_TRIGGERS:
            try:
                # Pass the channel's recent transcript so SagaStore's
                # contextual rewrite (gated by saga.toml's
                # [retrieval].enable_contextual_rewrite — default True
                # for prod homes) can resolve referential queries
                # ("yes, look for that") into self-contained retrieval
                # anchors before embedding/FTS. ``None`` channel
                # (system / poller triggers without a channel) skips
                # the rewrite naturally — context will be empty.
                rewrite_context = _rewrite_context_from_buffer(
                    self._buffer, event.channel_id,
                ) if event.channel_id else None
                payload = await self._saga.query(
                    event.content,
                    top_k=12,
                    session_id=saga_session_id,
                    context=rewrite_context,
                )
                raw_block = _format_saga_payload(payload)
                if raw_block and raw_block != "(no atoms)":
                    memory_block = raw_block
                ids = _atom_ids_from_response(payload)
                triple_ids = _source_atom_ids_from_triples(payload)
                seen: set[str] = set()
                for aid in list(ids) + list(triple_ids):
                    if aid not in seen:
                        seen.add(aid)
                        saga_atom_ids.append(aid)
            except Exception as exc:
                log.warning("pre-message saga.query failed: %s", exc)

        # Drain any pending subagent completion notifications from
        # prior turns on this channel — SPEC §4.4. Empty list → block
        # is None (build_turn_prompt skips the section).
        pending_subagents = await self._inbox.drain(event.channel_id or "")
        subagent_block = (
            render_subagent_updates(pending_subagents)
            if pending_subagents else None
        )

        # Per-turn prompt assembly — Recent activity, Recent feedback,
        # Session summaries, Resource usage, Upcoming, Upcoming
        # commitments, Self-state, etc. Synthesis turns
        # (saga_session_end) get a dedicated template instead.
        turn_prompt, recent = await self._build_turn_prompt(
            ctx, event,
            saga_block=memory_block,
            subagent_block=subagent_block,
        )

        # Build / reuse the agent singleton.
        agent = await self._build_agent_if_needed()

        # 0.3.0: auto-dispatch removed. The model's final text is NOT shipped
        # to the channel — the agent must call the ``send_message`` tool to
        # reply (and may call it multiple times per turn). The unsent final
        # text is captured as reasoning in the turn record.
        # ``turn_is_interactive`` feeds the forgot-to-send guard at turn end;
        # the typing indicator is started/cancelled in ``run_turn``.
        turn_is_interactive = is_interactive_turn(
            event.channel_id, event.trigger, self._channels
        )

        timeout = self._config.turn_timeout_seconds
        # asyncio.timeout() is available on Python 3.11+, which is the
        # hard deployment floor for mimir. Using it directly (no hasattr
        # guard) means a mis-deployed Python <3.11 crashes loudly with
        # AttributeError at turn-time rather than silently skipping
        # enforcement — a P0 safety mechanism should never degrade
        # silently. 0 = disabled (bench/dev).
        if timeout > 0:
            _timeout_ctx: Any = asyncio.timeout(timeout)
        else:
            _timeout_ctx = contextlib.nullcontext()

        error: str | None = None
        messages: list[Any] = []
        output = ""
        # chainlink #376 (PR 3/4): (event, fold_monotonic) for each message
        # folded into THIS turn mid-stream, read from the registry in the finally
        # below (before deactivate pops it) so the turn record can carry the
        # mid-turn inputs WITH a start-relative t_ms. Empty unless a mid-turn
        # message was actually folded. (Chat-history recording happens at inject
        # time via the dispatcher's on_inject hook, not here — PR 4.)
        folded_records: list[tuple[AgentEvent, float]] = []
        # chainlink #384: (event, reason) for folded messages the agent chose to
        # DEFER (via defer_injected_message) — re-enqueued as their own turns in
        # the finally, and marked deferred=true in injected_inputs below.
        deferred_records: list[tuple[AgentEvent, str]] = []
        try:
            async with _timeout_ctx:
                # ``stream_mode="values"`` yields the full state snapshot
                # after each graph step; ``state.get("messages", [])`` is
                # the canonical message list. We drain the stream to drive the
                # graph to completion, then derive events/output from the final
                # message list. (Pre-0.3.0 this loop also fed each new
                # AIMessage through a streaming auto-dispatcher that flushed
                # plan text mid-turn; auto-dispatch is gone — the agent now
                # delivers via the send_message tool.)
                invoke_config = {
                    "configurable": {
                        "thread_id": saga_session_id or session_id,
                        # Per-turn channel id so send_message / react /
                        # fetch_channel_history can default to it when
                        # the model doesn't supply ``channel_id``
                        # explicitly. Threads through LangGraph instead
                        # of the old process-global ``_STATE`` setter,
                        # which raced across concurrent dispatcher turns
                        # on different channels.
                        "channel_id": event.channel_id,
                    },
                }
                async for chunk in agent.astream(
                    {"messages": [HumanMessage(content=turn_prompt)]},
                    config=invoke_config,
                    stream_mode="values",
                ):
                    messages = list(chunk.get("messages", []))
                events, output = extract_turn_events(messages)
        except asyncio.TimeoutError:
            error = f"TurnTimeout: turn exceeded {timeout}s wall-clock limit"
            events = []
            log.error(
                "turn timed out after %ss (channel=%s, turn=%s)",
                timeout, event.channel_id, turn_id,
            )
            await log_event("turn_timeout", channel_id=event.channel_id, timeout_s=timeout)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            events = []
            log.exception("agent.astream failed: %s", exc)
            # Mid-turn quota exhaustion handling (SPEC §4.9 / §16 item 18).
            # If the model call surfaced as a 429, record a pause so
            # subsequent scheduled ticks suppress until the window
            # resets, and emit a structured ``quota_exhausted`` event
            # (algedonic negative). User-message turns still try to
            # run per §4.9 — they'll get their own 429 and surface
            # it to the operator via send_message rather than vanishing.
            try:
                from .quota_pause import QuotaPauseTracker, is_quota_exhaustion
                if is_quota_exhaustion(exc):
                    tracker = QuotaPauseTracker(
                        self._config.home / ".mimir" / "quota_pause.json"
                    )
                    # Classify: an authoritative reset → pause until then;
                    # a header-less 429 → short escalating backoff (so a
                    # transient burst doesn't sit out a full window).
                    reset_at, pause_reason = tracker.record_rate_limit(exc)
                    await log_event(
                        "quota_exhausted",
                        channel_id=event.channel_id,
                        turn_id=turn_id,
                        reset_at=reset_at.isoformat(),
                        pause_reason=pause_reason,
                        provider=tracker.provider,
                        exception_class=type(exc).__name__,
                        exception_message=str(exc)[:240],
                    )
                    # Arm a recovery wake so the agent retries exactly
                    # when the window should roll over, instead of idling
                    # until the next hourly scheduled tick.
                    sched = getattr(self, "_scheduler", None)
                    if sched is not None and hasattr(sched, "arm_quota_recovery_wake"):
                        try:
                            sched.arm_quota_recovery_wake(reset_at)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "arm_quota_recovery_wake failed; next "
                                "scheduled tick will still recover"
                            )
            except Exception:  # noqa: BLE001 — defensive boundary
                log.exception("quota_pause emit failed; continuing")
        finally:
            # chainlink #376: drop the in-flight registry entry (rejects a late
            # inject as no_active_turn; a crashed turn can't leak a stale active
            # queue). Any leftover events were accepted by inject_message AFTER
            # the turn's final before_model boundary (e.g. a follow-up sent while
            # the model was generating its last response) so they were never
            # folded. They arrived BEFORE any same-channel event that queued
            # while this turn ran, so route them to the FRONT of the channel
            # queue (ahead of those later events) rather than appending via
            # enqueue() — preserving within-channel arrival order (PR 2; mimir's
            # #591 + #593 review notes).
            # PR 3/4: snapshot what this turn folded (with fold times) BEFORE
            # deactivate pops the registry entry, so the record build below can
            # carry it with a t_ms. ``folded`` (consumed) and ``leftovers`` (never
            # folded) are disjoint — leftovers re-route as the next turn; folded
            # ones belong to this turn's durable record.
            folded_records = mid_turn_injection.folded_records(event.channel_id)
            # chainlink #384: read deferred records BEFORE deactivate pops the
            # entry (same reason as folded_records).
            deferred_records = mid_turn_injection.deferred_records(event.channel_id)
            leftover_injections = mid_turn_injection.deactivate(event.channel_id)
            if leftover_injections and self._dispatcher is not None:
                await log_event(
                    "mid_turn_injection_leftover",
                    channel_id=event.channel_id,
                    turn_id=turn_id,
                    count=len(leftover_injections),
                )
                self._dispatcher.requeue_front(leftover_injections)
            # chainlink #384: re-enqueue deferred messages as their own fresh
            # turns. force_new_turn=True makes Dispatcher.enqueue / startup-drain
            # refuse to re-fold them (loop guard); deferred_from_turn_id +
            # deferred_reason make the later delivery traceable. Front of the
            # queue (like leftovers) preserves their mid-turn arrival order ahead
            # of post-turn events. _buffer_recorded is preserved from the original
            # inject-time record, so chat history isn't duplicated.
            if deferred_records and self._dispatcher is not None:
                deferred_events = [
                    replace(ev, extra={
                        **ev.extra,
                        "force_new_turn": True,
                        "deferred_from_turn_id": turn_id,
                        "deferred_reason": reason,
                    })
                    for ev, reason in deferred_records
                ]
                await log_event(
                    "mid_turn_deferred",
                    channel_id=event.channel_id,
                    turn_id=turn_id,
                    count=len(deferred_events),
                )
                self._dispatcher.requeue_front(deferred_events)

        # Algedonic: surface EVERY turn failure as an event so a dropped
        # turn — a transient model 503, a timeout, quota exhaustion, or a
        # plain bug — is operator-visible on the ops dashboard and
        # queryable in events.jsonl, not just a ``log.exception`` line.
        # Fires for ALL turn kinds (poller / user_message / scheduled /
        # heartbeat) and ALL failure types; the ``turn_timeout`` and
        # ``quota_exhausted`` events above remain as additional context.
        # A transient poller-review 503 used to vanish entirely here —
        # invisible AND (cursor-advanced) un-retried. (chainlink #299)
        if error:
            await log_event(
                "turn_failed",
                channel_id=event.channel_id,
                turn_id=turn_id,
                trigger=event.trigger,
                error=error[:240],
                **_turn_outcome_identity(event),
            )
        elif event.trigger == "poller":
            # Success counterpart to ``turn_failed`` for poller turns
            # (chainlink #262): records that this poller item's turn was
            # processed without erroring, so the framework consumer can
            # advance the per-poller watermark past it and NOT re-emit it.
            # Poller-gated so events.jsonl doesn't grow a success event per
            # user / scheduled / heartbeat turn; not in
            # ``feedback._EVENT_RULES``, so it never surfaces algedonically
            # — it's a plumbing record for the poller-recovery consumer.
            await log_event(
                "turn_completed",
                channel_id=event.channel_id,
                turn_id=turn_id,
                trigger=event.trigger,
                **_turn_outcome_identity(event),
            )
        # chainlink #306: the terminal outcome for this turn has now been
        # emitted (turn_failed on a model-loop error, turn_completed on a
        # poller success). Mark it so run_turn's early-crash guard doesn't
        # double-emit if a LATER step in this body raises.
        ctx.outcome_emitted = True

        # Result fields drive the TurnRecord, so compute once and reuse.
        result_fields = derive_result_fields(messages)

        # Assemble this turn's cited-atom set for the TurnRecord. NO
        # automatic feedback (operator decision 2026-05-29): the old
        # post-message credit pass wrote a weight-2.0 ``feedback_positive``
        # boost on every cited atom whenever the turn merely "didn't fail"
        # — a positive-only ratchet layered on top of the ``retrieval``
        # access event recall already logs, crediting atoms for being in
        # the context window rather than for being used. Activation now
        # rises only from (a) that retrieval access event and (b)
        # DELIBERATE agent-curated feedback: the session-boundary synthesis
        # turn's ``saga_feedback`` votes and explicit
        # ``saga_mark_contributions`` (both emit ``saga_feedback_sent``).
        #
        # We still build the cited-atom union — pre-injected retrievals +
        # mid-turn ``saga_query`` hits + injected skill learnings (slice 6)
        # — because that's the candidate list the synthesis turn curates
        # over (it reaches the synthesis prompt via the TurnRecord's
        # ``saga_atom_ids`` → turns.jsonl → ``_atom_feedback_lines``).
        if self._saga is not None:
            tool_atom_ids = _extract_atom_ids_from_tool_results(messages)
            for aid in tool_atom_ids:
                if aid not in saga_atom_ids:
                    saga_atom_ids.append(aid)
            # Injected skill learnings only exist when a SagaStore is wired
            # (augment_skill_body needs one), so they belong under the same
            # guard — keeps the cited-atom assembly symmetric (#268).
            for aid in ctx.injected_skill_atom_ids:
                if aid not in saga_atom_ids:
                    saga_atom_ids.append(aid)

        # Build and write TurnRecord — matches the SDK schema.
        # Drain observability state captured during this turn:
        #   - permission_denials from WriteGuardBackend (every blocked
        #     write/edit/upload during the turn).
        #   - saga_calls from TurnContext (populated by
        #     RecordingSagaClient on every recorded saga method).
        #   - kind detection: scan events for spawn_claude_code tool
        #     calls; bench aggregate_usage keys on this for cost
        #     attribution (claude_code_spawn vs other).
        permission_denials: list = []
        if self._backend is not None and hasattr(self._backend, "drain_denials"):
            try:
                permission_denials = self._backend.drain_denials()
            except Exception as exc:  # noqa: BLE001
                log.debug("backend.drain_denials failed: %s", exc)
        # Gap 3 fix: emit one algedonic negative per denied write/edit/upload
        # so the per-turn feedback block surfaces write-guard denials. The
        # backend only records them (sync); we emit here where we're async.
        for _denial in permission_denials:
            await safe_log_event(
                "tool_call_denied",
                op=_denial.get("op"),
                file_path=_denial.get("file_path"),
                session_id=session_id,
            )
        # Gap 2 fix: if this is a synthesis turn (saga_session_end trigger)
        # and the model didn't call saga_end_session, emit the boundary-skip
        # signal so the next turn's algedonic block surfaces it for
        # self-correction.
        if event.trigger == "saga_session_end" and not ctx.saga_end_session_called:
            await safe_log_event(
                "saga_synthesis_skipped_boundary",
                session_id=saga_session_id,
                trigger=event.trigger,
            )
        # Expected-tool-call check (#234/#235; #299 follow-up; per-item in
        # #308): the event source can declare an ``expected_tool_call``
        # marker (a poller needing the turn to call a submission tool —
        # github-poller's review events expect ``gh pr review``). For a
        # poller BATCH each review item carries its OWN marker; when that
        # marker's ``bash_substrings`` is PR-specific (the PR number / url)
        # we can tell WHICH item wasn't submitted — so a duplicate review of
        # one PR no longer masks an unreviewed sibling (#308). A marker is
        # "missed" when NONE of its declared submissions fired.
        #
        # Only checked on a SUCCEEDED turn (#308 / finding #22): a failed or
        # timed-out turn already emits ``turn_failed``, so re-flagging the
        # un-submitted review would be redundant noise.
        sub_markers = [] if error else _expected_submission_markers(event.extra or {})
        if sub_markers:
            signal_type = sub_markers[0].get("signal_on_missing")
            # Per-item: each marker is satisfied independently by its own
            # submission(s). Generic (non-PR-specific) markers all match the
            # same submissions and degrade to all-or-nothing — no false
            # positives, just no per-item attribution.
            missed = [
                m for m in sub_markers
                if _count_expected_tool_calls(events, m) == 0
            ]
            if isinstance(signal_type, str) and signal_type.strip() and missed:
                ext = event.extra or {}
                items = ext.get("items") or []
                event_type = ext.get("event_type") or (
                    items[0].get("event_type")
                    if items and isinstance(items[0], dict) else None
                )
                # Which items were not submitted (PR url / #number) — for
                # PR-specific markers; None for generic markers.
                missed_refs = [m.get("ref") for m in missed if m.get("ref")]
                await safe_log_event(
                    signal_type.strip(),
                    channel_id=event.channel_id,
                    # ``event_type`` is log_event's positional parameter, so the
                    # originating poller event type goes under a distinct key —
                    # else "multiple values for argument 'event_type'" (the same
                    # collision pollers.py strips ``event_type`` to avoid).
                    source_event_type=event_type,
                    expected=len(sub_markers),
                    submitted=len(sub_markers) - len(missed),
                    missed=len(missed),
                    missed_refs=missed_refs or None,
                    expected_tool_names=list(sub_markers[0].get("tool_names") or []),
                )
        saga_calls = [
            {
                "call_type": c.call_type, "args": c.args, "result": c.result,
                "latency_ms": c.latency_ms, "error": c.error, "t_ms": c.t_ms,
            }
            for c in ctx.saga_calls
        ]
        kind: str | None = None
        for evt in events:
            if (
                isinstance(evt, dict)
                and evt.get("type") == "tool_call"
                and evt.get("name") == "spawn_claude_code"
            ):
                kind = "claude_code_spawn"
                break

        record = TurnRecord(
            ts=_utc_now(),
            turn_id=turn_id,
            session_id=session_id,
            saga_session_id=saga_session_id,
            trigger=event.trigger,
            channel_id=event.channel_id,
            input=truncate_input(turn_prompt),
            agent_id=self._config.agent_id,
            saga_atom_ids=saga_atom_ids,
            events=events,
            # chainlink #376 (PR 3/4): each mid-turn message folded into this turn
            # as ``{t_ms, text}`` — rendered the way the model saw it (author +
            # attachments header, capped) plus a start-relative offset (same axis
            # as event/saga t_ms) so the turn viewer can place it on the timeline
            # at the boundary it was folded, not in a side list.
            injected_inputs=[
                {
                    "t_ms": max(0, round((mono - t_total_start) * 1000, 2)),
                    "text": truncate_input(mid_turn_injection.render_injected_message(e)),
                    # chainlink #384: mark entries the agent deferred to their own
                    # turn, so "why didn't turn N answer this folded message?" is
                    # traceable from turn N's own record (pairs with the
                    # deferred_from_turn_id on the re-delivered turn).
                    **({"deferred": True}
                       if e.source_id in {ev.source_id for ev, _r in deferred_records}
                       else {}),
                }
                for e, mono in folded_records
            ],
            output=(output or "")[:2048],
            duration_ms=int((time.monotonic() - t_total_start) * 1000),
            error=error,
            permission_denials=permission_denials,
            saga_calls=saga_calls,
            kind=kind,
            **result_fields,
        )
        await self._turn_logger.write(record)

        # Fire the finalize stage of the turn hook chain. The default
        # ``CommitmentExtractionHook`` runs Phase 2a commitment
        # extraction (saga_session_end → structured commitment records),
        # restoring the SDK-era ``CommitmentExtractionHook.finalize``
        # behavior. Additional deployment-specific finalize hooks
        # (wiki backlinks, custom synthesis, etc.) can be registered
        # via the ``Agent(turn_hooks=...)`` constructor parameter or
        # ``Agent.add_hook(...)``. Per-hook exception isolation —
        # see ``mimir.turn_hooks.fire_hooks``.
        # chainlink #389: bound finalize hooks. Operator-registered hooks are
        # arbitrary code; a hang here would hold the dispatcher worker (and thus
        # the whole channel) forever — turn_timeout_seconds only covers the model
        # stream, not this post-loop work. The TurnRecord is already written
        # above, so a timeout here only drops best-effort finalize work.
        try:
            await asyncio.wait_for(
                fire_hooks("finalize", self._hooks, ctx, event, record),
                timeout=self._config.post_turn_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning(
                "finalize hooks exceeded post_turn_timeout (%ss) — skipped to "
                "avoid wedging the channel",
                self._config.post_turn_timeout_seconds,
            )

        # Post-turn observability hooks (181-M). Order matters:
        #   1. wiki_backlinks: regenerates state/wiki/{orphans,
        #      dangling-links,backlinks-index}.md when a wiki content
        #      page was modified this turn. Must run BEFORE index
        #      rebuild so INDEX.md picks up the freshly-regen'd outputs.
        #   2. index_rebuild: regen state/INDEX.md + memory/INDEX.md
        #      via the per-Agent IndexGenerator (debounced internally).
        #   3. git_commit: post-turn commit gated on
        #      MIMIR_GIT_TRACKING_ENABLED — runs last so all regen'd
        #      outputs are part of the same commit as the writes that
        #      triggered them.
        # Each is best-effort: failures log + return; turn record stays.
        await self._post_turn_wiki_backlinks(ctx)
        await self._post_turn_index_rebuild()
        await self._post_turn_git_commit(ctx)

        # 0.3.0: no auto-dispatch. The agent delivers via the send_message
        # tool only; the model's final text is captured as reasoning (it's
        # already in the TurnRecord ``output`` + a reasoning event) and is
        # intentionally NOT shipped to the channel or appended to the chat
        # buffer as a sent message. (Typing is released in run_turn's finally.)
        #
        # Forgot-to-send guard: an interactive turn that produced final text
        # but never DELIVERED a response means the text is stuck as reasoning
        # and the user got nothing. Key off confirmed delivery — a successful
        # send_message OR a successful react (a react-only acknowledgment is a
        # valid reply, so it must NOT be flagged) — NOT the presence of a tool
        # call, which can be refused (non-interactive / loop hard-stop / no
        # bridge) or soft-fail and deliver nothing. Emit a negative signal so
        # the next turn's feedback panel surfaces it (feedback.classify maps
        # ``interactive_turn_no_send_message`` to a negative ``no_reply``).
        if (
            turn_is_interactive
            and (output or "").strip()
            and getattr(ctx, "send_message_count", 0) == 0
            and getattr(ctx, "react_count", 0) == 0
        ):
            await safe_log_event(
                "interactive_turn_no_send_message",
                channel_id=event.channel_id,
                turn_id=turn_id,
                trigger=event.trigger,
                output_chars=len((output or "").strip()),
            )

        await log_event(
            "turn_finished",
            turn_id=turn_id,
            channel_id=event.channel_id,
            duration_ms=record.duration_ms,
            error=error,
            stop_reason=result_fields.get("stop_reason"),
        )

        # Autonomous-work triggers (cron + poller) don't anchor a
        # conversation, so the standard ``MIMIR_SAGA_SESSION_IDLE_MINUTES``
        # countdown would just defer the synthesis turn by 10 minutes with
        # no recall benefit (no more turns will fire on the same session
        # — the next cron tick / poller batch creates its own session via
        # ``SessionManager.touch``). Force-end the session now so the
        # synthesis turn runs immediately after the autonomous work.
        # ``end_now`` enqueues a ``saga_session_end`` turn via the registered
        # on-idle callback (see ``server.py:_on_session_idle``), which the
        # per-channel dispatcher will pick up after this turn returns and
        # the worker frees up.
        if (
            self._sessions is not None
            and event.channel_id
            and event.trigger in IMMEDIATE_SESSION_END_TRIGGERS
        ):
            try:
                await self._sessions.end_now(event.channel_id)
            except Exception:  # noqa: BLE001
                # Synthesis enqueue failure shouldn't crash the just-
                # completed autonomous turn — log + swallow. Note:
                # end_now pops the session from the registry under its
                # internal lock BEFORE calling _dispatch_idle, so an
                # exception here means the session is already ended
                # in-process state but the synthesis turn never got
                # enqueued. Recovery: the next event on the channel
                # creates a fresh session via touch() — the autonomous
                # work's session boundary atom is lost for this tick,
                # but the channel keeps functioning.
                log.exception(
                    "immediate session-end failed for trigger=%r channel_id=%r",
                    event.trigger,
                    event.channel_id,
                )

        return record

    # ────────────────────────────────────────────────────────────
    # Post-turn hooks (181-M)
    # ────────────────────────────────────────────────────────────
    #
    # ``_maybe_extract_commitments`` lived here pre-#213; it was
    # migrated to ``mimir.turn_hooks.CommitmentExtractionHook`` and
    # is now fired via the ``finalize`` stage of the turn hook chain
    # (see the ``await fire_hooks("finalize", ...)`` call above).
    # Other inlined finalize logic (wiki backlinks, etc.) can migrate
    # to hooks following the same pattern in follow-up PRs.

    # Generated wiki outputs — the WikiBacklinksHook regenerates these
    # itself, so changes to them must NOT trigger another regeneration
    # (otherwise the hook would loop on its own writes).
    _WIKI_GENERATED_OUTPUTS = frozenset({
        "orphans.md",
        "dangling-links.md",
        "backlinks-index.md",
    })

    def _snapshot_wiki_mtimes(self) -> dict[str, float]:
        """Walk ``<home>/state/wiki``, return ``{abs_path_str: st_mtime}``
        for every non-generated content page. Empty dict when the wiki
        dir doesn't exist."""
        wiki = self._config.home / "state" / "wiki"
        snapshot: dict[str, float] = {}
        if not wiki.is_dir():
            return snapshot
        for page in wiki.rglob("*.md"):
            if page.name in self._WIKI_GENERATED_OUTPUTS:
                continue
            try:
                snapshot[str(page)] = page.stat().st_mtime
            except OSError:
                continue
        return snapshot

    async def _post_turn_wiki_backlinks(self, ctx: Any) -> None:
        """Regenerate the wiki backlinks/orphans/dangling outputs when
        ANY non-generated state/wiki/*.md page changed mtime relative
        to the pre-turn snapshot. Edit-triggered (not periodic) so
        orphan / dangling regressions surface on the turn that
        introduced them. Direct port of ``WikiBacklinksHook.finalize``."""
        wiki = self._config.home / "state" / "wiki"
        if not wiki.is_dir():
            return
        before: dict[str, float] = getattr(ctx, "wiki_mtime_snapshot", {}) or {}
        after = self._snapshot_wiki_mtimes()

        touched = False
        for path_str, mtime in after.items():
            if before.get(path_str) != mtime:
                touched = True
                break
        if not touched:
            for path_str in before:
                if path_str not in after:
                    touched = True
                    break
        if not touched:
            return

        from . import wiki_backlinks
        try:
            await wiki_backlinks.run(self._config.home)
        except FileNotFoundError:
            # Wiki dir disappeared between snapshot and run — benign race.
            return
        except Exception:  # noqa: BLE001 — never crash a turn for this
            log.exception("wiki_backlinks regen failed; continuing")

    async def _post_turn_index_rebuild(self) -> None:
        """Mark INDEX.md dirty + flush the IndexGenerator. Debounced
        internally so consecutive turns within the debounce window
        share a single rebuild. Direct port of
        ``IndexRebuildHook.finalize``."""
        try:
            self._indexes.mark_dirty("all")
            await self._indexes.flush()
        except Exception:  # noqa: BLE001
            log.exception("INDEX rebuild flush failed; continuing")

    async def _post_turn_git_commit(self, ctx: Any) -> None:
        """Post-turn commit of memory/state changes, gated on
        ``MIMIR_GIT_TRACKING_ENABLED``. Runs after the index rebuild
        so auto-regenerated INDEX.md / wiki outputs are part of the
        same commit as the writes that triggered them. Failures inside
        ``commit_turn_changes`` are swallowed there and surfaced via
        ``git_commit_failed`` / ``git_push_failed`` algedonic events;
        we still wrap in try/except for defense in depth."""
        try:
            from . import git_tracking
            await git_tracking.commit_turn_changes(
                turn_id=ctx.turn_id,
                trigger=ctx.trigger,
                home=self._config.home,
                enabled=self._config.git_tracking_enabled,
            )
        except Exception:  # noqa: BLE001
            log.exception("git commit_turn_changes raised; continuing")

    # ────────────────────────────────────────────────────────────
    # Shell-job completion bridge
    # ────────────────────────────────────────────────────────────

    def _handle_shell_job_complete(self, job: Any) -> None:
        """Thread-safe bridge: a shell-job waiter thread invokes this
        when its subprocess exits. Schedules the async handler onto
        the captured asyncio loop so we can enqueue a
        ``shell_job_complete`` AgentEvent without crossing thread
        boundaries unsafely.

        Loop-unavailable path: shutdown / pre-first-turn / no
        dispatcher → log + drop. Operator eventually notices via
        ``bash_jobs_list``. No synthetic TurnRecord at risk; only
        the wake-up event for the spawning channel is lost.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            log.warning(
                "shell_job_complete dropped (loop unavailable): "
                "job_id=%s channel_id=%s",
                getattr(job, "job_id", "?"),
                getattr(job, "channel_id", None),
            )
            return
        if self._dispatcher is None:
            log.warning(
                "shell_job_complete dropped (no dispatcher wired): "
                "job_id=%s channel_id=%s",
                getattr(job, "job_id", "?"),
                getattr(job, "channel_id", None),
            )
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._on_shell_job_complete(job), loop,
            )
        except Exception:  # noqa: BLE001
            # Never let a daemon-thread invocation crash the registry.
            log.exception("schedule of shell-job-complete handler failed")

    async def _on_shell_job_complete(self, job: Any) -> None:
        """Async handler: build a turn-prompt summary of the job's exit
        state + bounded output tails and enqueue a
        ``shell_job_complete`` AgentEvent into the dispatcher. Routes
        back to the channel that spawned the job. No-channel jobs
        (e.g. spawned from a bare scheduled tick) are dropped — no
        sensible default routing target.
        """
        if getattr(job, "channel_id", None) is None:
            await log_event(
                "shell_job_complete_no_channel",
                job_id=job.job_id,
                exit_code=job.exit_code,
            )
            return

        try:
            data = self._shell_jobs.read_output(
                job.job_id, tail_lines=100, stream="both",
            )
        except Exception:  # noqa: BLE001
            data = {"stdout_tail": "", "stderr_tail": ""}

        stdout_tail = (data.get("stdout_tail") or "").strip()
        stderr_tail = (data.get("stderr_tail") or "").strip()
        # Bound each stream so a runaway job doesn't blow the prompt budget.
        max_chars = 4000
        if len(stdout_tail) > max_chars:
            stdout_tail = stdout_tail[-max_chars:]
        if len(stderr_tail) > max_chars:
            stderr_tail = stderr_tail[-max_chars:]

        elapsed = round(getattr(job, "elapsed_seconds", 0.0), 1)
        body_lines = [
            f"Shell job {job.job_id} complete (status={job.status}, "
            f"exit_code={job.exit_code}, elapsed={elapsed}s).",
            f"Command: {job.command}",
            "",
            "--- stdout tail ---",
            stdout_tail or "(empty)",
            "",
            "--- stderr tail ---",
            stderr_tail or "(empty)",
        ]
        body = "\n".join(body_lines)

        event = AgentEvent(
            trigger="shell_job_complete",
            channel_id=job.channel_id,
            content=body,
            source_id=f"shell_job:{job.job_id}",
            source="system",
            extra={"job_id": job.job_id, "exit_code": job.exit_code},
        )
        try:
            accepted = await self._dispatcher.enqueue(event)
        except Exception as exc:  # noqa: BLE001
            await log_event(
                "shell_job_complete_enqueue_failed",
                job_id=job.job_id,
                error=str(exc)[:500],
            )
            return
        await log_event("shell_job_complete_enqueue_ok", job_id=job.job_id)
        await log_event(
            "shell_job_complete_routed",
            job_id=job.job_id,
            channel_id=job.channel_id,
            exit_code=job.exit_code,
            accepted=accepted,
        )

    # ────────────────────────────────────────────────────────────
    # System prompt assembly
    # ────────────────────────────────────────────────────────────

    def _build_system_prompt(self, *, emit_health_events: bool = True) -> str:
        """Assemble the per-turn system prompt: persona + core memory +
        memory index + operator alert channel + skill catalog. Rebuilt
        each turn so skill bucket assignments / outcome counters stay
        current (chainlink #15: install-stable section comes first so
        the prompt cache prefix extends through it).

        ``emit_health_events`` lets the prompt-cache comparison path
        inspect the rendered prompt without writing ``core_prompt_degraded``
        on every unchanged turn; rebuilds enable it for the final prompt.

        Falls back to the minimal default prompt on any failure — a
        broken core-block read or skill-catalog crash should NEVER
        prevent a turn from running."""
        try:
            from .core_blocks import check_core_blocks_health, load_core
            from .prompts import build_system_prompt
            core_blocks = load_core(self._config.home)

            # S1-3: detect silent identity loss before it propagates.
            # Checks that enough core blocks loaded and none are stubs.
            # log_event_sync is used because _build_system_prompt is sync;
            # the async counterpart (log_event) would need an awaitable caller.
            degraded, issues = check_core_blocks_health(core_blocks)
            if degraded and emit_health_events:
                log.warning(
                    "core_prompt_degraded: %s — agent will use degraded prompt",
                    "; ".join(issues),
                )
                log_event_sync(
                    "core_prompt_degraded",
                    issues=issues,
                    block_count=len(core_blocks),
                )

            memory_index_body = (
                self._indexes.read_memory_index()
                if self._indexes is not None else None
            )
            # Skill catalog rendering moved to deepagents' SkillsMiddleware
            # (wired via ``create_deep_agent(skills=...)``). build_system_prompt
            # no longer composes a skill_block — the middleware injects
            # ``## Skills System`` into the prompt at request time.
            return build_system_prompt(
                core_blocks=core_blocks,
                memory_index_body=memory_index_body,
                operator_alert_channel=getattr(
                    self._config, "operator_alert_channel", "",
                ),
                skill_block=None,
                home_dir=str(self._config.home),
                writable_dirs=self._config.writable_dirs,
            )
        except Exception:
            log.exception("_build_system_prompt failed; using minimal default")
            return _DEFAULT_SYSTEM_PROMPT

    # NOTE: _assemble_skill_block was removed in the skills-middleware
    # restoration PR. The framework's SkillsMiddleware now renders the
    # catalog at request time via wrap_model_call — see the
    # ``create_deep_agent(skills=...)`` call in _get_or_build_agent.
    # Per-turn skill telemetry (the "(N/M in window)" counters) stays
    # in mimir's hands via _assemble_skill_telemetry_lines and the
    # ``## Self-state`` block.

    # ────────────────────────────────────────────────────────────
    # Per-turn block assembly (ported from main)
    # ────────────────────────────────────────────────────────────

    def _spawn_bg_task(self, coro):
        """Schedule a fire-and-forget coroutine on the running loop.

        No-op when there is no running loop (turns invoked from a sync
        test path) — caller's deferred event simply doesn't fire,
        which is fine because the per-turn block readers are
        non-essential.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _assemble_usage_block(
        self,
    ) -> tuple[str | None, list[tuple[str, dict]]]:
        """Aggregate over 1h / 5h / 7d, render the Resource usage prompt
        section, return ``(block_text, deferred_events)``.

        Side effects (deferred): cost_rate_alert / cost_rate_advisory
        + rate_limit_off_pace events under cooldown. Caller flushes
        deferred_events on the running loop because this method runs
        inside ``asyncio.to_thread``.
        """
        deferred: list[tuple[str, dict]] = []
        if not self._config.usage_block_enabled:
            return None, deferred

        from .stats_block import assemble_stats_block

        try:
            result = assemble_stats_block(
                self._config,
                self._rate_limits,
                turns_snapshot=self._turns_snapshot,
                events_snapshot=self._events_snapshot,
            )
        except Exception:  # noqa: BLE001
            log.exception("assemble_stats_block failed; skipping block")
            return None, deferred

        if result.alert is not None:
            from .billing import BillingMode
            advisory = self._config.billing_mode is BillingMode.QUOTA
            event_kind = "cost_rate_advisory" if advisory else "cost_rate_alert"
            if not event_recently_emitted(
                self._config.events_log,
                event_kind,
                cooldown_minutes=self._config.cost_alert_cooldown_minutes,
                snapshot=self._events_snapshot,
            ):
                deferred.append(
                    (
                        event_kind,
                        {
                            "reason": result.alert.reason,
                            "rate_now_usd_per_hour": round(
                                result.alert.rate_now_usd_per_hour, 4,
                            ),
                            "threshold_usd_per_hour": round(
                                result.alert.threshold_usd_per_hour, 4,
                            ),
                            "baseline_usd_per_hour": (
                                round(result.alert.baseline_usd_per_hour, 4)
                                if result.alert.baseline_usd_per_hour is not None
                                else None
                            ),
                        },
                    )
                )

        if result.off_pace and not event_recently_emitted(
            self._config.events_log,
            "rate_limit_off_pace",
            cooldown_minutes=self._config.cost_alert_cooldown_minutes,
            snapshot=self._events_snapshot,
        ):
            worst_key, worst_snap, worst_proj = result.off_pace[0]
            deferred.append(
                (
                    "rate_limit_off_pace",
                    {
                        "rate_limit_type": worst_key,
                        "utilization": worst_snap.utilization,
                        "on_pace_utilization": round(
                            worst_proj.on_pace_utilization, 4,
                        ),
                        "hours_until_reset": round(
                            worst_proj.hours_until_reset, 2,
                        ),
                        "resets_at": worst_snap.resets_at,
                    },
                )
            )

        return result.body, deferred

    def _assemble_upcoming_block(self) -> str | None:
        """v0.5+ §12.1: render the ``## Upcoming`` block."""
        try:
            from .upcoming import render_upcoming_block
            return render_upcoming_block(
                scheduler=self._scheduler,
                rate_limit_store=self._rate_limits,
            )
        except Exception:  # noqa: BLE001
            log.exception("_assemble_upcoming_block failed; skipping")
            return None

    def _assemble_commitments_block(
        self, channel_id: str | None,
    ) -> str | None:
        """Phase 3: ``## Upcoming commitments`` block — active records
        for this channel (+ unbound). Suppressed on synthetic
        scheduler:* / poller:* channels and when no store is wired."""
        if channel_id is None or self._commitments is None:
            return None
        from .history import SYNTHETIC_CHANNEL_PREFIXES
        if channel_id.startswith(SYNTHETIC_CHANNEL_PREFIXES):
            return None
        try:
            from .commitments.render import render_commitments_block
            records = self._commitments.list(
                channel_id=channel_id,
                include_unbound=True,
            )
            return render_commitments_block(records)
        except Exception:  # noqa: BLE001
            log.exception(
                "_assemble_commitments_block failed; skipping",
            )
            return None

    def _assemble_self_state_block(self) -> str | None:
        """v0.5+ §12.4: render the ``## Self-state`` block — homeostat
        view + uncommitted-files line + per-turn skill telemetry."""
        try:
            arbiter_body = self._arbiter.render_self_state_block()
        except Exception:  # noqa: BLE001
            log.exception(
                "_assemble_self_state_block (arbiter) failed; skipping",
            )
            arbiter_body = None
        git_line = self._assemble_git_status_line()
        skill_body = self._assemble_skill_telemetry_lines()
        parts = [s for s in (arbiter_body, git_line, skill_body) if s]
        if not parts:
            return None
        return "\n".join(parts)

    def _assemble_git_status_line(self) -> str | None:
        """``- uncommitted in /mimir-home: <count> file(s) — <topN>`` line.
        Suppressed when tracking is off, count==0, or summary errored.
        """
        if not self._config.git_tracking_enabled:
            return None
        try:
            return health.render_git_status_line(self._config.home)
        except Exception:  # noqa: BLE001
            log.exception("_assemble_git_status_line failed; skipping")
            return None

    def _assemble_skill_telemetry_lines(self) -> str | None:
        """Per-turn skill bucket telemetry lines for ``## Self-state``."""
        try:
            from .skill_outcomes import (
                aggregate,
                load_skill_success_criteria,
                render_skill_telemetry,
            )
            from .skill_defs import installed_skill_names
            seeded = installed_skill_names(self._config.home)
            if not seeded:
                return None
            # Per-skill success_criteria refines load-kind outcomes
            # into success vs incomplete based on whether the
            # operator-declared completion signals fired in the turn.
            # No-op for skills without a success_criteria block.
            criteria = load_skill_success_criteria(self._config.home)
            aggs = aggregate(
                self._config.turns_log, skill_criteria=criteria,
            )
            return render_skill_telemetry(seeded, aggs)
        except Exception:  # noqa: BLE001
            log.exception(
                "_assemble_skill_telemetry_lines failed; skipping",
            )
            return None

    async def _assemble_session_summaries(
        self, *, channel_id: str | None,
    ) -> str | None:
        """Render the Recent session summaries block from SagaStore's
        ``recent_session_boundaries()``."""
        count = self._config.recent_boundaries
        if count <= 0:
            return None
        boundaries: list[dict] = []
        if self._saga is not None:
            try:
                # Best-effort — SAGA client may not implement this. We
                # don't want to crash a turn over an optional method.
                recent_fn = getattr(
                    self._saga, "recent_session_boundaries", None,
                )
                if recent_fn is not None:
                    boundaries = await recent_fn(
                        channel_id=channel_id, count=count,
                    )
            except Exception:  # noqa: BLE001
                log.exception(
                    "_assemble_session_summaries: SAGA "
                    "recent_session_boundaries failed"
                )
                boundaries = []
        turn_counts: dict[str, int] = {}
        if channel_id is not None and boundaries:
            snapshot_records = self._turns_snapshot.records
            for b in boundaries:
                ts = str(b.get("ts") or b.get("timestamp") or "")
                if not ts:
                    continue
                turn_counts[ts] = count_turns_since(
                    self._config.turns_log,
                    channel_id=channel_id,
                    since_ts=ts,
                    snapshot_records=snapshot_records,
                )
        now = datetime.now(tz=timezone.utc)
        return render_session_summaries(
            boundaries,
            now=now,
            turn_counts=turn_counts,
            stale_age_hours=self._config.unfinished_stale_age_hours,
            stale_turns=self._config.unfinished_stale_turns,
        )

    async def _build_synthesis_prompt(
        self, ctx: Any, event: AgentEvent,
    ) -> str:
        """For trigger='saga_session_end' — load the synthesis template,
        embed the session's turn window from turns.jsonl. Off-loops the
        turns.jsonl scan via to_thread."""
        saga_session_id = ctx.saga_session_id or (event.extra or {}).get(
            "saga_session_id", "",
        )
        idle_minutes = self._config.saga_session_idle_minutes
        turns_window = await asyncio.to_thread(
            _filter_session_turns,
            self._config.turns_log,
            saga_session_id,
            idle_minutes=idle_minutes,
        )
        if not turns_window:
            self._spawn_bg_task(
                log_event(
                    "saga_synthesis_empty_window",
                    saga_session_id=saga_session_id,
                    channel_id=event.channel_id,
                    reason="turns.jsonl rotated past this session's records",
                )
            )
        return render_saga_session_end(
            channel_id=event.channel_id or "",
            saga_session_id=saga_session_id,
            idle_minutes=idle_minutes,
            turns_window=turns_window,
            prompts_dir=self._config.prompts_dir,
        )

    async def _build_turn_prompt(
        self,
        ctx: Any,
        event: AgentEvent,
        saga_block: str | None,
        subagent_block: str | None,
    ) -> tuple[str, list]:
        """Assemble the per-turn user-side prompt + the recent-message
        list. Synthesis turns get a dedicated synthesis prompt; everything
        else builds the standard turn prompt with the algedonic /
        session-summary / usage / upcoming / self-state blocks.

        Returns ``(turn_prompt, recent)`` — recent is needed for the
        ``turn_started`` event's ``recent_message_count``.
        """
        if event.trigger == "saga_session_end":
            return await self._build_synthesis_prompt(ctx, event), []

        recent = self._buffer.assemble_recent_activity(
            channel_id=event.channel_id or "",
            author=event.author,
            recent_per_channel=self._config.recent_per_channel,
            recent_author_cross=self._config.recent_author_cross,
            cross_hours=self._config.recent_cross_hours,
            source_allowlist=self._config.recent_sources,
        )
        feedback_block = (
            self._feedback.recent_block()
            if self._config.feedback_limit_per_polarity > 0
            else None
        )
        # Open change-proposal nudge (chainlink #337/#339/#344), rendered next
        # to the feedback signals so the agent finishes/abandons an in-flight
        # proposal. Sync git call off the loop; a prompt section must never
        # break the turn, hence the broad guard.
        try:
            from .proposals import render_open_proposals_block
            core_proposals_block = await asyncio.to_thread(
                render_open_proposals_block, self._config.home
            )
        except Exception:  # noqa: BLE001 — prompt assembly must not fail a turn
            core_proposals_block = None
        session_summaries_block = await self._assemble_session_summaries(
            channel_id=event.channel_id,
        )
        usage_block, deferred_usage_events = await asyncio.to_thread(
            self._assemble_usage_block
        )
        # Flush deferred events on the running loop.
        from .ntfy import fire_cost_runaway_alarm_if_warranted
        for event_kind, event_kwargs in deferred_usage_events:
            self._spawn_bg_task(log_event(event_kind, **event_kwargs))
            # Dead-man alarm: cost-rate runaway (chainlink #66). Fires an ntfy
            # push when the hourly rate exceeds the runaway threshold so the
            # operator is notified even when not watching chat.
            self._spawn_bg_task(
                fire_cost_runaway_alarm_if_warranted(event_kind, event_kwargs)
            )
        upcoming_block = self._assemble_upcoming_block()
        commitments_block = self._assemble_commitments_block(
            channel_id=event.channel_id,
        )
        self_state_block = await asyncio.to_thread(
            self._assemble_self_state_block,
        )
        # Auto-surface the relevant SKILL.md when this turn is on a
        # ``poller:<name>`` channel — the agent gets the skill's
        # content inline without needing a ``find-skills`` lookup.
        # Closes the failure mode where the agent reaches for
        # ``send_message`` instead of the skill-specific workflow
        # because it never loaded the matching skill (muninn-mimir
        # 2026-05-23: a Bluesky reply landed in Discord). Non-poller
        # turns return None and the prompt is unaffected.
        from .skill_resolver import find_skill_for_channel
        from .skill_defs import home_builtin_skills_dir, home_skills_dir
        # Disk I/O (walk skills dirs + read pollers.json + SKILL.md)
        # wrapped in to_thread to match the existing pattern in this
        # function — every other file-touching call here uses
        # asyncio.to_thread. Consistency note from PR #315 review.
        # Cost is sub-ms at current skill counts (<20 bundled + a few
        # operator-installed) but keeps the event loop honest if skill
        # counts grow or a SKILL.md gets large.
        skills_dirs = (
            home_skills_dir(self._config.home),
            home_builtin_skills_dir(self._config.home),
        )
        auto_skill_block = await asyncio.to_thread(
            find_skill_for_channel,
            event.channel_id,
            skills_dirs,
        )
        # chainlink #266: when a skill auto-loads, its accumulated
        # learnings (gotchas/tips stored as skill_learning atoms) load
        # with it — appended under the same Skill section. Best-effort:
        # only when a concrete SagaStore is wired; recall errors leave the
        # body unchanged (skill load must not fail on a memory miss).
        if auto_skill_block is not None and self._saga_store is not None:
            from . import skill_memory
            _skill_name, _skill_body = auto_skill_block
            _conn = self._saga_store.connection()
            _augmented, _injected_ids = await asyncio.to_thread(
                skill_memory.augment_skill_body, _conn, _skill_name, _skill_body,
            )
            auto_skill_block = (_skill_name, _augmented)
            # slice 6: record the injected learnings as this turn's cited
            # atoms so the session-boundary synthesis turn curates feedback
            # on them (run_turn folds ctx.injected_skill_atom_ids into the
            # TurnRecord's saga_atom_ids).
            for _aid in _injected_ids:
                if _aid not in ctx.injected_skill_atom_ids:
                    ctx.injected_skill_atom_ids.append(_aid)
        # Channel memory injection (chainlink #187): load per-channel fact
        # files (operator name, preferences, patterns) from
        # ``memory/channels/<channel_id>/``.  Returns None for synthetic
        # channels (scheduler:*, poller:*) and channels with no memory files.
        from .core_blocks import load_channel_memory
        channel_memory_block = await asyncio.to_thread(
            load_channel_memory,
            self._config.home,
            event.channel_id or "",
        )
        turn_prompt = build_turn_prompt(
            event,
            recent_messages=recent,
            saga_block=saga_block,
            subagent_block=subagent_block,
            recent_message_chars=self._config.recent_message_chars,
            resolver=self._buffer.resolver,
            feedback_block=feedback_block,
            core_proposals_block=core_proposals_block,
            session_summaries_block=session_summaries_block,
            usage_block=usage_block,
            upcoming_block=upcoming_block,
            commitments_block=commitments_block,
            self_state_block=self_state_block,
            auto_skill_block=auto_skill_block,
            saga_session_id=ctx.saga_session_id,
            channel_memory_block=channel_memory_block,
        )
        return turn_prompt, recent
