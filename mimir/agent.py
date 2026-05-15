"""mimir.Agent — deepagents-backed.

Post-cutover (2026-05-14): replaces the 2459-LOC SDK-backed
Agent class with a thin wrapper around LangGraph's deepagents.
The legacy implementation is preserved at
``mimir/agent_sdk_legacy.py.bak`` for reference; will be deleted
in a follow-up cleanup pass.

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
from .models import AgentEvent, TurnRecord
from .saga_client import SagaClient
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
# Model / tool resolution helpers (lifted from deepagent_poc)
# ────────────────────────────────────────────────────────────────────


def _resolve_model(spec: str | BaseChatModel) -> str | BaseChatModel:
    """Translate a mimir-friendly model spec to a deepagents-acceptable form.

    Supported:
      - ``claude-code:<model>`` → ChatClaudeCode (Max OAuth subprocess)
      - ``<provider>:<model>``  → init_chat_model via deepagents
      - BaseChatModel instance  → pass-through (Bedrock/Vertex/etc.)
    """
    if isinstance(spec, BaseChatModel):
        return spec
    if not isinstance(spec, str):
        raise TypeError(f"unexpected model spec type: {type(spec).__name__}")
    if spec.startswith("claude-code:"):
        from langchain_claude_code import ChatClaudeCode  # type: ignore[import-untyped]
        model_name = spec.split(":", 1)[1]
        return ChatClaudeCode(model=model_name)
    return spec


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
        self._loop: asyncio.AbstractEventLoop | None = None

        # Stubs for legacy server.py reads — post-cutover these are
        # not the agent's responsibility (rate-limit tracking moved
        # to the oauth_usage_poller's own store). Stubbed to a tiny
        # no-op so server.py's wiring code doesn't blow up.
        self._rate_limits = _RateLimitStub()

        # Build the deepagent singleton. Done lazily to keep import-time
        # fast and to let tests construct Agent without a real model.
        self._agent: Any | None = None

        # Memory-tool dep injection — only used if saga_client is a
        # MemoryClient (post-saga cutover). Wires up the @tool's
        # MemoryClient handle so deepagents can call into recall.
        if saga_client is not None:
            self._try_inject_memory_client(saga_client)

    def _try_inject_memory_client(self, saga_client: SagaClient) -> None:
        """If saga_client is a MemoryClient (or wraps one), wire it
        into the memory_query / memory_store tools.

        Production saga_client is a RecordingSagaClient wrapping
        either _InProcessSaga (legacy) or MemoryClient. We unwrap one
        level to find the concrete client.
        """
        try:
            from .memory.client import MemoryClient
        except Exception:
            return
        candidate: Any = saga_client
        # RecordingSagaClient stores the inner under _inner.
        inner = getattr(candidate, "_inner", None)
        if inner is not None:
            candidate = inner
        # _MemoryStateProxy / similar — find the actual client.
        if isinstance(candidate, MemoryClient):
            from .deepagent_poc.memory_tool import set_memory_client
            set_memory_client(candidate)

    def _build_agent_if_needed(self) -> Any:
        if self._agent is not None:
            return self._agent
        from deepagents import create_deep_agent
        from .deepagent_poc.memory_tool import memory_query
        from .deepagent_poc.store_tool import memory_store

        model_spec = os.environ.get("MIMIR_MODEL_SPEC", "claude-code:claude-sonnet-4-6")
        system_prompt = os.environ.get(
            "MIMIR_SYSTEM_PROMPT_OVERRIDE",
            _DEFAULT_SYSTEM_PROMPT,
        )
        self._agent = create_deep_agent(
            model=_resolve_model(model_spec),
            tools=[memory_query, memory_store],
            system_prompt=system_prompt,
        )
        return self._agent

    async def run_turn(self, event: AgentEvent) -> TurnRecord:
        """Run one agent turn — preserves the SDK Agent.run_turn contract."""
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

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

        # Pre-message memory inject.
        from .sagatools import _format_saga_payload, _atom_ids_from_response, _source_atom_ids_from_triples
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
        agent = self._build_agent_if_needed()

        error: str | None = None
        messages: list[Any] = []
        output = ""
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=prompt)]},
                config={
                    "configurable": {"thread_id": saga_session_id or session_id},
                },
            )
            messages = result.get("messages", [])
            events, output = extract_turn_events(messages)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            events = []
            log.exception("agent.ainvoke failed: %s", exc)

        # Post-message credit pass.
        if error is None and saga_atom_ids and self._saga is not None:
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
                    feedback="positive",
                )
            except Exception as exc:
                log.warning("post-message saga.feedback failed: %s", exc)

        # Build and write TurnRecord — matches the SDK schema.
        result_fields = derive_result_fields(messages)
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

        log_event(
            "turn_finished",
            turn_id=turn_id,
            channel_id=event.channel_id,
            duration_ms=record.duration_ms,
            error=error,
            stop_reason=result_fields.get("stop_reason"),
        )
        return record
