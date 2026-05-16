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
from langchain_core.messages import HumanMessage

from .channel_registry import ChannelRegistry
from .config import Config
from .event_logger import log_event
from .history import MessageBuffer
from .index import IndexGenerator
from . import _langchain_claude_code_patches as _lcc_patches
from .models import AgentEvent, TurnRecord
from .saga_client import SagaClient

# Idempotent runtime patch for langchain-claude-code's ``_arun`` call —
# see the module docstring for the bug + upstream PR. No-op if the
# claude-code extra isn't installed.
_lcc_patches.apply_patches()
from .sagatools import (
    _atom_ids_from_response,
    _format_saga_payload,
    _source_atom_ids_from_triples,
)
from .search import Indexer
from .session_manager import SessionManager
from .subagent_inbox import SubagentInbox
from .turn_logger import (
    TurnLogger,
    derive_result_fields,
    extract_turn_events,
    make_turn_id,
    truncate_input,
)

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Stubs for legacy server.py reads
# ────────────────────────────────────────────────────────────────────


class _RateLimitStub:
    """No-op replacement for agent._rate_limits, which used to capture
    SDK RateLimitEvent stream events. Post-cutover the oauth_usage_poller
    owns its own RateLimitStore; this stub keeps server.py wiring code
    from KeyError'ing during build_app."""
    async def update_from_event(self, *args, **kwargs) -> None:
        return None

    def snapshot(self) -> dict:
        return {}

    async def append(self, *args, **kwargs) -> None:
        return None


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
        return ChatClaudeCode(model=model_name)
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

        # Stub for back-compat with any caller that still reads
        # ``agent._rate_limits``. Post-cutover the oauth_usage_poller
        # owns the real RateLimitStore directly (server.py wires it).
        # When all callers are updated, drop this entirely.
        self._rate_limits = _RateLimitStub()

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
        ctx = _TurnContext(
            turn_id=turn_id,
            session_id=session_id,
            trigger=event.trigger,
            channel_id=event.channel_id,
            started_at=t_total_start,
            saga_session_id=saga_session_id,
        )
        ctx_token = set_current_turn(ctx)
        try:
            return await self._run_turn_body(
                event, ctx, ctx_token, turn_id, session_id, saga_session_id,
                t_total_start,
            )
        finally:
            reset_current_turn(ctx_token)

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

        # Pre-message memory inject.
        memory_block = ""
        saga_atom_ids: list[str] = []
        if self._saga is not None:
            try:
                payload = await self._saga.query(
                    event.content,
                    top_k=12,
                    session_id=saga_session_id,
                )
                memory_block = _format_saga_payload(payload)
                ids = _atom_ids_from_response(payload)
                triple_ids = _source_atom_ids_from_triples(payload)
                seen: set[str] = set()
                for aid in list(ids) + list(triple_ids):
                    if aid not in seen:
                        seen.add(aid)
                        saga_atom_ids.append(aid)
            except Exception as exc:
                log.warning("pre-message saga.query failed: %s", exc)

        prompt = event.content
        if memory_block and memory_block != "(no atoms)":
            prompt = (
                f"## Possibly relevant memories (from SAGA)\n\n{memory_block}\n\n"
                f"---\n\n{event.content}"
            )

        # Build / reuse the agent singleton.
        agent = await self._build_agent_if_needed()

        error: str | None = None
        messages: list[Any] = []
        output = ""
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=prompt)]},
                config={
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
                },
            )
            messages = result.get("messages", [])
            events, output = extract_turn_events(messages)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            events = []
            log.exception("agent.ainvoke failed: %s", exc)

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
            input=truncate_input(prompt),
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

        # Bridge to channel out (send_message). Default sends the
        # reply text to the originating channel. Bridge-specific
        # logic that lived in the SDK Agent (typing indicator,
        # streaming dispatch) is dropped for now — Phase D re-add.
        if (
            self._channels is not None
            and event.channel_id
            and output
            and event.trigger == "user_message"
        ):
            bridge = self._channels.find(event.channel_id)
            if bridge is not None and hasattr(bridge, "send"):
                try:
                    await bridge.send(event.channel_id, output)
                except Exception as exc:
                    log.warning("bridge.send failed: %s", exc)

        await log_event(
            "turn_finished",
            turn_id=turn_id,
            channel_id=event.channel_id,
            duration_ms=record.duration_ms,
            error=error,
            stop_reason=result_fields.get("stop_reason"),
        )
        return record

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
            )
        except Exception:
            log.exception("_build_system_prompt failed; using minimal default")
            return _DEFAULT_SYSTEM_PROMPT

    def _assemble_skill_block(self) -> str | None:
        """v0.5+ §12.3: render the install-stable skill catalog for the
        system prompt. Returns None when no skills are seeded; volatile
        per-turn telemetry (success/total counts) is handled separately
        via the self-state block (deferred to Phase D)."""
        try:
            from .skill_outcomes import SkillPinConfig, render_skill_catalog
            from .skill_defs import installed_skill_names
            seeded = installed_skill_names(self._config.home)
            if not seeded:
                return None
            pin = SkillPinConfig.load(
                self._config.home / "state" / "skill-pin.yaml",
            )
            return render_skill_catalog(seeded, pin)
        except Exception:
            log.exception("_assemble_skill_block failed; skipping")
            return None
