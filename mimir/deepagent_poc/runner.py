"""DeepAgentRunner — the deepagent-backed replacement for mimir's
``Agent`` class (mimir/agent.py:723).

This is the production-cutover target shape. Sketched here in the
PoC; full migration would land this as ``mimir/runner.py`` (or
replace ``mimir/agent.py:Agent`` in place).

What ``mimir/agent.py:Agent`` does today (2459 LOC):
  - Owns ``ClientPool`` of ``ClaudeSDKClient`` instances (~250 LOC)
  - Per-turn ``ClaudeAgentOptions`` fingerprinting + recycling
  - SDK lifecycle (connect, disconnect, stale-mark, idle cleanup)
  - Hook orchestration (HookMatcher chains)
  - Stream event consumption (StreamEvent / ResultMessage / etc.)
  - Prompt assembly (system + user with memory / feedback /
    self-state / usage blocks)
  - Session-store delete after each turn
  - Subagent spawn classification + cost capture
  - Dispatcher integration (enqueue, drain, loop-detect)

What disappears with deepagents:
  - ClientPool — CompiledStateGraph is thread-safe; one singleton
    shared across concurrent turns (~250 LOC negative)
  - SDK message-type telemetry — turn_logger adapter walks
    LangChain messages instead (~50 LOC swap, no net change)
  - Per-turn SessionStore.delete() — LangGraph handles state
    isolation via per-call ``thread_id`` in ``config``
  - Options-fingerprint recycling — model is bound at agent
    construction; switching providers means constructing a new
    DeepAgentRunner (cheap, no pool needed)
  - HookMatcher chains — replaced by the external wrapper pattern
    (run_pre_message + run_post_message)

What stays:
  - Prompt assembly (system + user; we built run_pre_message for
    the memory injection part)
  - Subagent spawn (deepagents has a built-in ``task`` tool — but
    mimir's spawn.py has bespoke cost-capture; need to bridge)
  - Dispatcher / channel registry / scheduler / session manager —
    these are mimir's own infra, agnostic to which agent runtime
    they call. DeepAgentRunner gets the same constructor deps as
    Agent does today.

Production cutover plan:
  1. Wire DeepAgentRunner alongside Agent (feature flag in
     mimir/server.py:build_app — `MIMIR_USE_DEEPAGENTS=1` picks the
     new path)
  2. Run a 50-q via_mimir bench through DeepAgentRunner; verify
     parity with the SDK-backed bench (the 98% single-session-user
     baseline)
  3. Migrate dispatcher / channel calls site-by-site
  4. Delete Agent class + ClientPool + claude_agent_sdk imports
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from mimir.config import Config
from mimir.memory.client import MemoryClient
from .agent import resolve_model, SYSTEM_PROMPT
from .memory_tool import memory_query, set_memory_client
from .store_tool import memory_store
from .turn_logger import TurnLogger
from .turn_runner import TurnOutcome, run_turn


@dataclass
class AgentEvent:
    """Mirrors mimir's AgentEvent (mimir/models.py). The dispatcher
    constructs these from inbound channel events / scheduler ticks /
    subagent completions and hands them to ``run_turn``."""
    trigger: str            # "user_message" | "scheduled_tick" | etc.
    content: str            # the user message / synthesis prompt
    channel_id: str | None
    author: str | None = None
    extra: dict[str, Any] | None = None


class DeepAgentRunner:
    """Thin wrapper that owns a singleton CompiledStateGraph and
    exposes a ``run_turn(event)`` method matching mimir's existing
    Agent.run_turn contract.

    Construction:
        runner = DeepAgentRunner(
            config=Config.from_env(),
            memory_client=memory_client,
            turn_logger=turn_logger,
            sessions=session_manager,
            channels=channel_registry,
            dispatcher=dispatcher,
            scheduler=scheduler,
            model="claude-code:claude-sonnet-4-6",  # or any spec
        )

    Per-event:
        outcome = await runner.run_turn(event)

    The agent is built ONCE in __init__ — no pool, no recycling.
    LangGraph handles per-call isolation via ``thread_id`` in the
    invoke config.
    """

    def __init__(
        self,
        *,
        config: Config,
        memory_client: MemoryClient,
        turn_logger: TurnLogger,
        # The constructor deps mimir's Agent already takes — we
        # forward them unchanged because dispatcher / channel /
        # session / scheduler are runtime-agnostic.
        session_manager: Any = None,
        channel_registry: Any = None,
        dispatcher: Any = None,
        scheduler: Any = None,
        subagent_inbox: Any = None,
        # New: model spec passed to make_agent. Defaults to mimir's
        # current default. The string form covers openai/anthropic/
        # claude-code; pass a BaseChatModel instance for bespoke
        # providers (Bedrock, Vertex, etc.).
        model: str | BaseChatModel = "claude-code:claude-sonnet-4-6",
        tools: list[Any] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._config = config
        self._memory_client = memory_client
        self._turn_logger = turn_logger
        self._sessions = session_manager
        self._channels = channel_registry
        self._dispatcher = dispatcher
        self._scheduler = scheduler
        self._inbox = subagent_inbox

        # Wire memory tool's module-state. (In a full migration this
        # would happen at app boot, not per-runner; here for PoC
        # clarity.)
        set_memory_client(memory_client)

        # Build the deepagent ONCE. CompiledStateGraph is thread-safe
        # so we share across all concurrent turns. The ClientPool from
        # mimir/agent.py is unnecessary on this surface.
        from deepagents import create_deep_agent
        self._agent = create_deep_agent(
            model=resolve_model(model),
            tools=tools or [memory_query, memory_store],
            system_prompt=system_prompt or SYSTEM_PROMPT,
        )

    async def run_turn(self, event: AgentEvent) -> TurnOutcome:
        """Run one agent turn for an inbound ``AgentEvent``.

        Mirrors mimir's existing ``Agent.run_turn(event) -> TurnRecord``
        contract; returns a ``TurnOutcome`` envelope (includes the
        TurnRecord plus pre/post telemetry).

        Hooks for session_id / saga_session_id / reference_date pull
        from the existing SessionManager + per-turn context resolution
        — same plumbing mimir already has, just feeding into a
        different runtime.
        """
        # Session attach — mirrors mimir/agent.py:2294-2299
        session_id = event.channel_id or "default"
        saga_session_id: str | None = None
        if event.trigger == "saga_session_end":
            extra = event.extra or {}
            saga_session_id = extra.get("saga_session_id")
        elif self._sessions is not None:
            sess = await self._sessions.touch(event.channel_id)
            saga_session_id = sess.saga_session_id
            self._sessions.increment_turn_count(event.channel_id)

        # Recent-channel context for the contextual-rewrite path
        # (mirrors mimir/agent.py's recent-window assembly)
        context_messages: list[dict[str, str]] = []
        # Production: pull recent messages from self._buffer for the
        # contextual-rewrite path. PoC leaves empty.

        outcome = await run_turn(
            self._agent,
            memory_client=self._memory_client,
            question=event.content,
            session_id=session_id,
            channel_id=event.channel_id,
            saga_session_id=saga_session_id,
            context_messages=context_messages or None,
            trigger=event.trigger,
            turn_logger=self._turn_logger,
            config={
                # LangGraph per-call thread isolation. Re-uses the
                # turn ID (generated inside run_turn) wouldn't work
                # because we don't have it yet; use saga_session_id
                # as a stable per-channel anchor.
                "configurable": {
                    "thread_id": saga_session_id or session_id,
                },
            },
        )
        return outcome

    async def close(self) -> None:
        """Release any resources. CompiledStateGraph doesn't hold
        external connections — this is essentially a no-op vs the
        SDK's ClientPool.drain()."""
        # No pool to drain. Memory client / turn logger / etc. are
        # owned elsewhere; closing them is the caller's job.
        pass


# ────────────────────────────────────────────────────────────────────
# Code-comparison: what mimir/agent.py would shed
# ────────────────────────────────────────────────────────────────────
#
# from mimir/agent.py, these sections would be deleted on cutover:
#
#   line 199-565: ClientPool + _PoolEntry + _AcquireContext  (~370 LOC)
#   line 41-53:   claude_agent_sdk imports                    (~10 LOC)
#   line 565-720: _LegacyClientProxy + shutdown_sdk_client    (~155 LOC)
#   line 825-960: hook chains (SubagentLifecycleHook,
#                 CancelTypingHook, etc.)                    (~135 LOC)
#   line 1000+:   stream event handler / ResultMessage parser (~varies)
#
# Net: ~700-900 LOC of agent.py becomes DeepAgentRunner's ~150 LOC
# above, plus the turn_runner.run_turn (~150 LOC) it composes.
#
# What stays in mimir/agent.py (or moves to a new module):
#   - Prompt assembly (build_turn_prompt + system prompt) — both still
#     needed; deepagents takes the system prompt + we have run_pre_message
#     for the user message memory injection
#   - Dispatcher / channel / session manager wiring — unchanged
#   - Spawn classification (mimir/spawn.py) — needs to bridge to
#     deepagents' built-in ``task`` tool
