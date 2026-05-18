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
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from .bridges._directives import parse_directives, ReactDirective
from .channel_registry import ChannelRegistry
from .config import Config
from .event_logger import log_event
from .feedback import FeedbackLog
from . import health
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
    SessionBoundaryLog,
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
from .sagatools import (
    _atom_ids_from_response,
    _format_saga_payload,
    _source_atom_ids_from_triples,
)
from .search import Indexer
from .session_manager import SessionManager
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
    {"saga_session_end", "scheduled_tick", "poller"}
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
    {"scheduled_tick", "poller"}
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
    "claude-code": "claude-code",  # → pip install 'mimir[claude-code]'
    "anthropic": "anthropic",
    "openai": "openai",
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


def _resolve_model(
    spec: str | BaseChatModel,
    *,
    max_retries: int = 6,
) -> BaseChatModel:
    """Translate a mimir-friendly model spec into a constructed BaseChatModel.

    Supported:
      - ``claude-code:<model>`` → ChatClaudeCode (Max OAuth subprocess)
      - ``<provider>:<model>``  → init_chat_model with ``max_retries`` (and,
                                  for OpenAI hitting api.openai.com,
                                  ``use_responses_api=True``)
      - BaseChatModel instance  → pass-through (Bedrock/Vertex/custom)

    ``max_retries`` only applies to the non-claude-code path — ChatClaudeCode
    spawns a Claude Code subprocess which handles its own retry semantics.

    The model-provider package (``langchain-claude-code``,
    ``langchain-anthropic``, etc.) is a pip extra (see pyproject.toml's
    ``[project.optional-dependencies]``). We lazy-import here so installing
    only the extras you'll use keeps the dep graph small — raising a
    clear hint on ImportError tells the operator exactly which extra
    they're missing.
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
                "MIMIR_MODEL_SPEC=claude-code:* requires the 'claude-code' extra. "
                "Install via `pip install 'mimir[claude-code]'` "
                "(or `uv pip install langchain-claude-code`)."
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
        return ChatClaudeCode(
            model=model_name,
            permission_mode="bypassPermissions",
        )
    # langchain ``init_chat_model`` resolves provider extras at call time
    # (``anthropic:`` → langchain-anthropic, ``openai:`` → langchain-openai).
    # We wrap it here so we can thread max_retries + the responses-API flag
    # through; if the extra isn't installed, init_chat_model raises with
    # the right hint.
    from langchain.chat_models import init_chat_model
    init_params: dict[str, Any] = {"max_retries": max(0, int(max_retries))}
    if spec.startswith("openai:") and _supports_responses_api():
        init_params["use_responses_api"] = True
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


# Default system prompt — production cutover replaces this with mimir's
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
    ) -> None:
        self._config = config
        self._turn_logger = turn_logger
        self._buffer = message_buffer
        self._indexes = index_generator
        self._indexer = indexer
        self._saga = saga_client
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
        )
        self._session_boundary_log = SessionBoundaryLog(
            path=config.home / ".mimir" / "session_boundaries.jsonl",
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
        from .billing import AnthropicQuotaProvider, BillingMode, QuotaProvider
        from .budget import HomeostaticArbiter
        quota_providers: list[QuotaProvider] = []
        if config.billing_mode is BillingMode.QUOTA:
            quota_providers.append(AnthropicQuotaProvider(self._rate_limits))
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
        self._agent_lock: asyncio.Lock | None = None
        self._backend: Any | None = None

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
        """
        if not event.content or not event.channel_id:
            return
        if event.trigger in self._INBOUND_SKIP_TRIGGERS:
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
        except Exception:  # noqa: BLE001
            log.exception(
                "inbound buffer append failed for event trigger=%r channel=%r",
                event.trigger, event.channel_id,
            )

    async def _append_outbound_to_buffer(
        self,
        channel_id: str,
        content: str,
        *,
        msg_id: str | None = None,
        source: str | None = None,
    ) -> None:
        """Append an outbound assistant message to the buffer. Called
        from every ``bridge.send`` site (agent-fallback, ``send_message``
        tool, streaming dispatcher) so the agent sees its own prior
        replies in ``## Recent activity`` on the next turn.

        Called whether or not delivery succeeded — pre-#181's
        ``_auto_dispatch_or_record`` recorded outbound regardless of
        dispatch outcome so the agent self-corrects when a stale
        conversation doesn't match what it thought it sent.
        """
        if not channel_id or not content:
            return
        try:
            msg = self._buffer.make_message(
                channel_id=channel_id,
                kind="assistant_message",
                content=content,
                msg_id=msg_id,
                source=source,
            )
            await self._buffer.append(msg)
        except Exception:  # noqa: BLE001
            log.exception(
                "outbound buffer append failed for channel=%r", channel_id,
            )

    async def _build_agent_if_needed(self) -> Any:
        # Double-checked init: fast path returns the cached agent
        # without acquiring the lock; only the first-call window
        # contends on the asyncio.Lock. Pre-fix two concurrent first
        # turns each entered the ``is None`` branch and built their
        # own CompiledStateGraph — the second one won, the first was
        # GC'd, both paid the import cost.
        if self._agent is not None:
            return self._agent
        if self._agent_lock is None:
            self._agent_lock = asyncio.Lock()
        async with self._agent_lock:
            # Re-check under the lock: a contending turn may have just
            # finished construction while we waited.
            if self._agent is not None:
                return self._agent
            from deepagents import create_deep_agent
            from .readonly_backend import WriteGuardBackend
            from .tools import all_mimir_tools

            # Config carries the operator-set model spec; env override
            # exists for ad-hoc bench / smoke runs that don't go through
            # Config.from_env. See Config.model_spec for the format
            # (``claude-code:<model>`` or ``<provider>:<model>``).
            model_spec = os.environ.get(
                "MIMIR_MODEL_SPEC",
                getattr(self._config, "model_spec", "claude-code:claude-sonnet-4-6"),
            )
            # Assemble the real system prompt — core memory + memory index +
            # operator alert channel + skill catalog. Built fresh per turn
            # so skill bucket assignments / outcome aggregates stay current
            # (chainlink #15: install-stable section comes first for cache).
            system_prompt = os.environ.get(
                "MIMIR_SYSTEM_PROMPT_OVERRIDE",
                self._build_system_prompt(),
            )
            # Per-directory write-permission enforcement (Config.folders).
            # Read tools (Glob/Grep/Read) stay unrestricted; Write/Edit/upload
            # outside ``writable_dirs`` return a permission error instead of
            # mutating the filesystem. ``.mimir/`` (saga db, metrics) is
            # implicitly blocked because it's not in the folders dict.
            backend = WriteGuardBackend(
                root_dir=self._config.home,
                writable_dirs=self._config.writable_dirs,
            )
            # Stored so run_turn can drain recorded denials into the
            # TurnRecord.permission_denials field at end of turn.
            self._backend = backend
            self._agent = create_deep_agent(
                model=_resolve_model(
                    model_spec,
                    max_retries=getattr(self._config, "model_max_retries", 6),
                ),
                tools=all_mimir_tools(),
                system_prompt=system_prompt,
                backend=backend,
            )
            return self._agent

    async def run_turn(self, event: AgentEvent) -> TurnRecord:
        """Run one agent turn — preserves the SDK Agent.run_turn contract."""
        turn_id = make_turn_id()
        t_total_start = time.monotonic()
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

        # Typing indicator at turn start — Discord/Slack bridges expose
        # ``send_typing_indicator`` so the user sees "mimir is typing…"
        # while a multi-second LLM call runs. Pre-fix the indicator
        # only fired on the post-turn ``bridge.send``; long turns
        # appeared hung. Bridges that don't implement the method
        # (Bluesky / Bench / WebChat) are silently skipped.
        if (
            self._channels is not None
            and event.channel_id
            and event.trigger == "user_message"
        ):
            bridge = self._channels.find(event.channel_id)
            if bridge is not None and hasattr(bridge, "send_typing_indicator"):
                try:
                    await bridge.send_typing_indicator(event.channel_id)
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
        from .tools.registry import set_current_channel_id as _set_cid
        _set_cid(event.channel_id)
        try:
            return await self._run_turn_body(
                event, ctx, ctx_token, turn_id, session_id, saga_session_id,
                t_total_start,
            )
        finally:
            reset_current_turn(ctx_token)
            _set_cid(None)

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
                payload = await self._saga.query(
                    event.content,
                    top_k=12,
                    session_id=saga_session_id,
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

        # Streaming auto-dispatcher (181-O / chainlink #5): observes
        # AIMessages as they stream in and flushes the "plan" text to
        # the channel at the first tool_call boundary. Bench / no-
        # bridge channels skip; explicit ``send_message`` tool calls
        # disable streaming entirely. The result text is flushed at
        # end of turn via the existing bridge.send path below — when
        # streaming was active we use ``dispatcher.result_text()``
        # instead of the canonical ``output`` so intermediate text
        # between tool calls is correctly suppressed from the user.
        bridge_for_streaming = None
        if self._channels is not None and event.channel_id:
            bridge_for_streaming = self._channels.find(event.channel_id)
        from ._streaming_dispatch import StreamingAutoDispatcher
        streaming = StreamingAutoDispatcher(
            channel_id=event.channel_id or "",
            bridge=bridge_for_streaming,
            # Mid-turn plan flushes append to the chat-history buffer
            # so the agent sees its own streamed reply in the next
            # turn's Recent activity (just like the end-of-turn send).
            outbound_appender=self._append_outbound_to_buffer,
            channel_source=ctx.channel_source,
            eligible=(
                event.channel_id is not None
                and event.trigger == "user_message"
            ),
        )

        error: str | None = None
        messages: list[Any] = []
        output = ""
        try:
            # ``stream_mode="values"`` yields the full state snapshot
            # after each graph step; ``state.get("messages", [])`` is
            # the canonical message list. Feed each NEW AIMessage
            # through the streaming state machine so mid-turn plan
            # flushes happen at the first tool_call boundary —
            # NOT at end of turn.
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
            # Track the count of AIMessages we've already streamed
            # into the state machine. ``stream_mode="values"``
            # re-emits the cumulative messages list on every step,
            # so without slice-past-observed we'd feed the same
            # AIMessages in multiple times. The state machine's
            # ``observed_count`` is bumped per-call, so we feed
            # only the new AIMessages each iteration.
            ai_observed = 0
            async for chunk in agent.astream(
                {"messages": [HumanMessage(content=turn_prompt)]},
                config=invoke_config,
                stream_mode="values",
            ):
                messages = list(chunk.get("messages", []))
                if streaming.enabled:
                    # Walk messages, ignore any non-AIMessage, slice
                    # past the ones already observed.
                    ai_count = 0
                    for msg in messages:
                        if isinstance(msg, AIMessage):
                            ai_count += 1
                            if ai_count > ai_observed:
                                await streaming.observe(msg)
                    ai_observed = ai_count
            events, output = extract_turn_events(messages)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            events = []
            log.exception("agent.astream failed: %s", exc)

        # Result fields drive both the TurnRecord and the feedback-signal
        # branch below, so compute once and reuse.
        result_fields = derive_result_fields(messages)

        # Post-message credit pass. Branch on result: a successful turn
        # contributes positive evidence ("these atoms helped the agent
        # answer"); an errored or max_turns-truncated turn is negative
        # evidence ("retrieval surfaced these but the agent couldn't
        # land an answer"). Saga's record_outcome routes the signal to
        # the activation-log weight accordingly.
        if saga_atom_ids and self._saga is not None:
            stop_reason = result_fields.get("stop_reason")
            is_failure = (
                error is not None
                or result_fields.get("result_is_error")
                or stop_reason in ("max_turns", "max_tokens")
            )
            feedback_signal = "negative" if is_failure else "positive"
            try:
                # Union: pre-message atoms + atom IDs surfaced in tool results
                tool_atom_ids = _extract_atom_ids_from_tool_results(messages)
                seen2 = set(saga_atom_ids)
                for aid in tool_atom_ids:
                    if aid not in seen2:
                        seen2.add(aid)
                        saga_atom_ids.append(aid)
                await self._saga.feedback(
                    saga_atom_ids,
                    output,
                    session_id=saga_session_id,
                    feedback=feedback_signal,
                )
            except Exception as exc:
                log.warning("post-message saga.feedback failed: %s", exc)

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
            saga_atom_ids=saga_atom_ids,
            events=events,
            output=(output or "")[:2048],
            duration_ms=int((time.monotonic() - t_total_start) * 1000),
            error=error,
            permission_denials=permission_denials,
            saga_calls=saga_calls,
            kind=kind,
            **result_fields,
        )
        await self._turn_logger.write(record)

        # Phase 2a commitment extraction — on saga_session_end synthesis
        # turns, extract structured commitments from the synthesis output
        # via a haiku-tier ``query()`` call and persist net-new ones to
        # ``CommitmentsStore``. Pre-181-I this lived in
        # ``CommitmentExtractionHook`` on the SDK turn-lifecycle chain;
        # the deepagents agent has no equivalent hook chain so the call
        # is inlined here. Best-effort: failures log + continue, the
        # turn record is unaffected.
        await self._maybe_extract_commitments(ctx, event, record)

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

        # End-of-turn bridge dispatch. Three cases:
        #   1. Streaming was active AND a plan was already flushed
        #      mid-turn (``streamed_plan=True``): send the result text
        #      via ``dispatcher.result_text()`` — intermediate text
        #      between tool calls is correctly suppressed from the
        #      user, captured only as reasoning in turns.jsonl.
        #   2. Streaming was disabled by an explicit ``send_message``
        #      tool call (the canonical-delivery path): skip the
        #      end-of-turn send entirely; the model already shipped.
        #   3. Otherwise (no tool calls, or streaming disabled because
        #      bench/no-bridge): single canonical ``output`` flush —
        #      matches pre-181-O behavior.
        if (
            self._channels is not None
            and event.channel_id
            and event.trigger == "user_message"
        ):
            bridge = self._channels.find(event.channel_id)
            if bridge is not None and hasattr(bridge, "send"):
                # Pick the right text + decide whether to send at all.
                send_text: str | None = None
                if streaming.disabled_by_explicit_send:
                    send_text = None  # already delivered via tool call
                elif streaming.streamed_plan:
                    candidate = streaming.result_text()
                    send_text = candidate if candidate else None
                elif output:
                    send_text = output
                if send_text:
                    parsed = parse_directives(send_text)
                    clean = parsed.clean_text
                    sent_result = None
                    if clean:
                        try:
                            sent_result = await bridge.send(event.channel_id, clean)
                        except Exception as exc:
                            log.warning("bridge.send failed: %s", exc)
                        # Append to chat-history buffer regardless of
                        # send outcome — pre-#181's _auto_dispatch_or_record
                        # explicitly recorded outbound even on bridge
                        # failure so the agent self-corrects when a
                        # stale conversation doesn't match what it
                        # thought it sent (matches what was attempted,
                        # not what was confirmed delivered).
                        await self._append_outbound_to_buffer(
                            event.channel_id,
                            clean,
                            msg_id=getattr(sent_result, "message_id", None),
                            source=ctx.channel_source,
                        )
                    for _directive in parsed.directives:
                        if isinstance(_directive, ReactDirective):
                            _target = _directive.message_id or (
                                sent_result.message_id if sent_result else None
                            )
                            try:
                                await bridge.react(
                                    event.channel_id, _target, _directive.emoji
                                )
                            except Exception as exc:
                                log.debug("bridge.react (directive) failed: %s", exc)
                elif streaming.streamed_plan:
                    # Edge case: a plan was flushed mid-turn with
                    # final=False (typing indicator held), but the
                    # model produced no result text after the last
                    # tool call. Without an end-of-turn send the
                    # indicator dangles in "still working" forever
                    # (~9s on Discord, then auto-expires; longer on
                    # Slack). Release it explicitly via the bridge's
                    # cancel_typing API. Failures swallowed — typing
                    # state is observability, not load-bearing.
                    if hasattr(bridge, "cancel_typing"):
                        try:
                            await bridge.cancel_typing(event.channel_id)
                        except Exception as exc:
                            log.debug("bridge.cancel_typing failed: %s", exc)

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

    async def _maybe_extract_commitments(
        self, ctx: Any, event: AgentEvent, record: TurnRecord,
    ) -> None:
        """Phase 2a: extract commitments from a saga_session_end synthesis
        output and persist net-new records to the CommitmentsStore.

        Direct port of ``CommitmentExtractionHook.finalize`` from main —
        the SDK-era hook chain is gone, so the deepagents agent runs
        this inline after writing the TurnRecord. Best-effort throughout:
        every failure path logs + returns; the synthesis turn's own
        record is unaffected.

        Events emitted:
          - ``commitments_extracted`` on ≥1 added record (carries count,
            skipped_dedupe, prompt_version)
          - ``commitments_extraction_no_op`` with reason in
            {short_output, llm_returned_zero, all_dedupe_skipped} when
            the path ran but added nothing — distinguishable in
            backtests from "skipped extraction entirely."

        Suppressed when:
          - ``ctx.trigger != "saga_session_end"`` (this hook is
            synthesis-only — the per-message extraction path is a
            different design).
          - ``self._commitments is None`` (test harnesses construct
            the Agent without a store).
          - ``record.output`` is empty (LLM returned nothing or the
            ainvoke errored).
        """
        if ctx.trigger != "saga_session_end":
            return
        if self._commitments is None:
            return
        output = getattr(record, "output", "") or ""
        if not output:
            return

        from .commitments.extractor import (
            EXTRACTION_PROMPT_VERSION,
            MIN_OUTPUT_LEN,
            extract_commitments,
        )

        # Synthetic channels (``scheduler:*`` / ``poller:*``) are never
        # delivery targets for the commitment poller or prompt-block
        # surfacing. If a saga_session_end fires on a synthetic channel
        # (e.g. a heartbeat tick that called saga_end_session) and the
        # LLM marks any item ``channel_bound=True``, the resulting record
        # would be bound to ``scheduler:heartbeat`` / ``poller:X`` —
        # permanently unreachable because _assemble_commitments_block
        # suppresses rendering on synthetic channels entirely.
        # Fix: nullify the channel before passing to the extractor.
        # All commitments from synthetic-channel sessions become unbound
        # (channel_id=None) and surface cross-channel as intended.
        # Log events below keep ctx.channel_id for observability (origin
        # channel), not for binding — no change there.
        from .history import SYNTHETIC_CHANNEL_PREFIXES
        effective_channel_id = ctx.channel_id
        if (
            effective_channel_id
            and effective_channel_id.startswith(SYNTHETIC_CHANNEL_PREFIXES)
        ):
            effective_channel_id = None

        if len(output) < MIN_OUTPUT_LEN:
            await log_event(
                "commitments_extraction_no_op",
                reason="short_output",
                output_len=len(output),
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
            return

        try:
            extracted = await extract_commitments(
                output,
                channel_id=effective_channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
            )
        except Exception:  # noqa: BLE001 — best-effort, never crash a turn
            log.exception(
                "commitment extraction failed for turn %s; skipping",
                ctx.turn_id,
            )
            return

        if not extracted:
            await log_event(
                "commitments_extraction_no_op",
                reason="llm_returned_zero",
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
            return

        # Snapshot dedupe keys once — N×|JSONL| in the prior shape,
        # N + |JSONL| this way. ``current_state`` returns active records
        # only, matching the find_by_dedupe_key semantics.
        state = self._commitments.current_state()
        existing_keys = {
            r.dedupe_key
            for r in state.values()
            if r.dedupe_key and not r.is_terminal()
        }

        added = 0
        skipped_dedupe = 0
        for rec in extracted:
            if rec.dedupe_key in existing_keys:
                skipped_dedupe += 1
                continue
            try:
                await self._commitments.add(rec)
                added += 1
                existing_keys.add(rec.dedupe_key)
            except Exception:  # noqa: BLE001
                log.exception(
                    "commitments store.add failed for record %s", rec.id,
                )

        if added > 0:
            await log_event(
                "commitments_extracted",
                count=added,
                skipped_dedupe=skipped_dedupe,
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
        else:
            await log_event(
                "commitments_extraction_no_op",
                reason="all_dedupe_skipped",
                extracted_count=len(extracted),
                skipped_dedupe=skipped_dedupe,
                channel_id=ctx.channel_id,
                saga_session_id=getattr(ctx, "saga_session_id", None),
                source_turn_id=ctx.turn_id,
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )

    # ────────────────────────────────────────────────────────────
    # Post-turn hooks (181-M)
    # ────────────────────────────────────────────────────────────

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

    def _build_system_prompt(self) -> str:
        """Assemble the per-turn system prompt: persona + core memory +
        memory index + operator alert channel + skill catalog. Rebuilt
        each turn so skill bucket assignments / outcome counters stay
        current (chainlink #15: install-stable section comes first so
        the prompt cache prefix extends through it).

        Falls back to the minimal default prompt on any failure — a
        broken core-block read or skill-catalog crash should NEVER
        prevent a turn from running."""
        try:
            from .core_blocks import load_core
            from .prompts import build_system_prompt
            core_blocks = load_core(self._config.home)
            memory_index_body = (
                self._indexes.read_memory_index()
                if self._indexes is not None else None
            )
            skill_block = self._assemble_skill_block()
            return build_system_prompt(
                core_blocks=core_blocks,
                memory_index_body=memory_index_body,
                operator_alert_channel=getattr(
                    self._config, "operator_alert_channel", "",
                ),
                skill_block=skill_block,
                home_dir=str(self._config.home),
            )
        except Exception:
            log.exception("_build_system_prompt failed; using minimal default")
            return _DEFAULT_SYSTEM_PROMPT

    def _assemble_skill_block(self) -> str | None:
        """v0.5+ §12.3: render the install-stable skill catalog for the
        system prompt. Returns None when no skills are seeded; volatile
        per-turn telemetry (success/total counts) is handled separately
        via the self-state block (deferred to Phase D).

        Each rendered line is ``- <name> — <one-line trigger>`` when
        SKILL.md frontmatter declares one; otherwise bare ``- <name>``.
        Trigger phrases come from the home-seeded ``<home>/.claude/
        skills/`` copy first, with a fallback to the bundled root for
        any skill not yet copied — covers fresh-install homes before
        ``seed_skills`` runs. SKILL.md frontmatter is install-stable,
        so description content doesn't perturb the prompt cache
        (chainlink #15)."""
        try:
            from .skill_outcomes import SkillPinConfig, render_skill_catalog
            from .skill_defs import installed_skill_names
            from .skill_catalog import load_catalog, DEFAULT_SKILLS_ROOT
            seeded = installed_skill_names(self._config.home)
            if not seeded:
                return None
            pin = SkillPinConfig.load(
                self._config.home / "state" / "skill-pin.yaml",
            )
            descriptions: dict[str, str] = {}
            try:
                home_skills = self._config.home / ".claude" / "skills"
                for entry in load_catalog(home_skills):
                    if entry.trigger:
                        descriptions[entry.name] = entry.trigger
                    elif entry.description:
                        descriptions[entry.name] = entry.description
                for entry in load_catalog(DEFAULT_SKILLS_ROOT):
                    if entry.name in descriptions:
                        continue
                    if entry.trigger:
                        descriptions[entry.name] = entry.trigger
                    elif entry.description:
                        descriptions[entry.name] = entry.description
            except Exception:  # noqa: BLE001
                # Description load is best-effort; on any failure fall
                # back to the bare-names rendering.
                log.exception("skill description load failed; rendering names only")
                descriptions = {}
            return render_skill_catalog(seeded, pin, descriptions=descriptions)
        except Exception:
            log.exception("_assemble_skill_block failed; skipping")
            return None

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
                SkillPinConfig, aggregate, render_skill_telemetry,
            )
            from .skill_defs import installed_skill_names
            seeded = installed_skill_names(self._config.home)
            if not seeded:
                return None
            aggs = aggregate(self._config.turns_log)
            pin = SkillPinConfig.load(
                self._config.home / "state" / "skill-pin.yaml"
            )
            return render_skill_telemetry(seeded, aggs, pin)
        except Exception:  # noqa: BLE001
            log.exception(
                "_assemble_skill_telemetry_lines failed; skipping",
            )
            return None

    async def _assemble_session_summaries(
        self, *, channel_id: str | None,
    ) -> str | None:
        """Render the Recent session summaries block. Tries SAGA first
        (chronological recall via /v1/sessions/recent); falls back to
        the local mirror on empty / failure."""
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
                    "recent_session_boundaries failed; falling back to "
                    "local mirror"
                )
                boundaries = []
        if not boundaries:
            boundaries = self._session_boundary_log.recent(
                channel_id=channel_id, count=count,
            )
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
        session_summaries_block = await self._assemble_session_summaries(
            channel_id=event.channel_id,
        )
        usage_block, deferred_usage_events = await asyncio.to_thread(
            self._assemble_usage_block
        )
        # Flush deferred events on the running loop.
        for event_kind, event_kwargs in deferred_usage_events:
            self._spawn_bg_task(log_event(event_kind, **event_kwargs))
        upcoming_block = self._assemble_upcoming_block()
        commitments_block = self._assemble_commitments_block(
            channel_id=event.channel_id,
        )
        self_state_block = await asyncio.to_thread(
            self._assemble_self_state_block,
        )
        turn_prompt = build_turn_prompt(
            event,
            recent_messages=recent,
            saga_block=saga_block,
            subagent_block=subagent_block,
            recent_message_chars=self._config.recent_message_chars,
            resolver=self._buffer.resolver,
            feedback_block=feedback_block,
            session_summaries_block=session_summaries_block,
            usage_block=usage_block,
            upcoming_block=upcoming_block,
            commitments_block=commitments_block,
            self_state_block=self_state_block,
            saga_session_id=ctx.saga_session_id,
        )
        return turn_prompt, recent
