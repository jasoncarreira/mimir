"""Sketch: production cutover wiring in mimir/server.py.

This is NOT a runnable module — it's the diff-shape for what
mimir/server.py:build_app would become to switch between the SDK-
backed ``Agent`` and the deepagents-backed ``DeepAgentRunner``.

The cutover lands behind a feature flag so we can A/B both paths and
delete Agent only after parity is proven.

------------------------------------------------------------------
Reference: current mimir/server.py:build_app lines 240-300 builds:
  - turn_logger / message_buffer / indexes / indexer
  - saga_client = make_saga_client(...)
  - sessions / inbox / channels / dispatcher / scheduler
  - agent = Agent(config, turn_logger, message_buffer, ..., dispatcher=...)
  - dispatcher.set_run_turn(agent.run_turn)
------------------------------------------------------------------

The cutover replaces the ``agent = Agent(...)`` block with a flag-
gated path:

```python
# In mimir/server.py:build_app, replace the existing agent construction
# (around line 285) with:

import os
from mimir.config import Config
from mimir.deepagent_poc.runner import DeepAgentRunner  # PoC path
# Eventually moves to mimir.runner.DeepAgentRunner

use_deepagents = os.environ.get("MIMIR_USE_DEEPAGENTS") == "1"

if use_deepagents:
    # Build the deepagent-backed runner. saga_client is replaced by
    # MemoryClient (already wired post-cutover; see saga_client.py
    # changes in feat/mimir-memory-tier3-triples-twm branch).
    from mimir.memory.client import MemoryClient

    memory_client = MemoryClient(
        db_path=config.home / ".mimir" / "memory.db",
        embedding_dim=None,  # auto-detect from saga.toml
    )
    agent = DeepAgentRunner(
        config=config,
        memory_client=memory_client,
        turn_logger=turn_logger,
        session_manager=sessions,
        channel_registry=channels,
        dispatcher=dispatcher,
        scheduler=scheduler,
        subagent_inbox=inbox,
        model=os.environ.get(
            "MIMIR_MODEL_SPEC",
            "claude-code:claude-sonnet-4-6",  # Max OAuth default
        ),
    )
else:
    # Current SDK-backed path (unchanged)
    agent = Agent(
        config, turn_logger, message_buffer, indexes,
        indexer=indexer,
        saga_client=saga_client,
        session_manager=sessions,
        scheduler=scheduler,
        subagent_inbox=inbox,
        channel_registry=channels,
        dispatcher=dispatcher,
    )

dispatcher.set_run_turn(agent.run_turn)
```

------------------------------------------------------------------
Migration sequence:
------------------------------------------------------------------

Phase A: ship the flag (low risk, default OFF)
  1. Land mimir/runner.py (DeepAgentRunner moved out of deepagent_poc/)
  2. Land the flag-gated branch above
  3. Default ``MIMIR_USE_DEEPAGENTS`` unset → SDK path; production
     unaffected
  4. Devs set the flag in their local .env to dogfood

Phase B: bench parity (the gating evidence)
  1. Run a 50-q via_mimir bench with MIMIR_USE_DEEPAGENTS=1
  2. Compare to the SDK baseline (98% single-session-user, current)
  3. Goal: hit ≥95% on the same questions
  4. If gap: diagnose tool / prompt / hook differences

Phase C: production rollout (gradual)
  1. Flip MIMIR_USE_DEEPAGENTS=1 in production .env for one channel
     first (e.g., bench-only channel)
  2. Monitor: turn errors, tool call rates, response latency, cache
     hit rate from langchain-openai usage metrics
  3. Roll out to additional channels after 48h clean run
  4. Make ``MIMIR_USE_DEEPAGENTS=1`` the default

Phase D: delete the SDK path
  1. After 1 week production stability: remove the ``use_deepagents``
     branch — keep only DeepAgentRunner
  2. Delete mimir/agent.py:Agent class (~2k LOC)
  3. Delete claude_agent_sdk dependency
  4. Delete SDK-specific tests
  5. Land cleanup commit

Estimated wall-clock: 2-3 weeks for engineer-active work + 1 week
of canary observation during Phase C.

------------------------------------------------------------------
Test-migration sketch (one example):
------------------------------------------------------------------

tests/test_agent_saga.py today asserts the SDK's behavior at
specific call sites (lines 12-20):

```python
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ContentBlock,
    HookContext, HookMatcher, ResultMessage, TextBlock, ToolUseBlock,
)
```

Post-cutover equivalent:

```python
from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
from mimir.runner import DeepAgentRunner, AgentEvent  # or mimir.deepagent_poc.runner

async def test_deep_agent_runner_invokes_memory_query(monkeypatch, tmp_path):
    from mimir.memory.client import MemoryClient
    from mimir.deepagent_poc.turn_logger import TurnLogger

    memory_client = MemoryClient(db_path=tmp_path / "memory.db",
                                  embedding_dim=None)
    # ... seed atoms via memory_client.store(...) ...

    turn_logger = TurnLogger(tmp_path / "turns.jsonl")
    runner = DeepAgentRunner(
        config=Config.from_env(),
        memory_client=memory_client,
        turn_logger=turn_logger,
        model="anthropic:claude-haiku-4-5",  # cheap for tests
    )

    event = AgentEvent(
        trigger="user_message",
        content="What's the user's favorite color?",
        channel_id="test-channel",
    )
    outcome = await runner.run_turn(event)

    # Test the canonical envelope, not SDK-specific message types
    assert outcome.error is None
    assert "blue" in outcome.output.lower()
    assert outcome.post_message.feedback_ok
    assert len(outcome.post_message.atom_ids_credited) > 0
    # Verify telemetry
    record = outcome.turn_record
    assert record.trigger == "user_message"
    assert any(e["type"] == "tool_call" for e in record.events)
```

Patterns:
- Construct DeepAgentRunner with cheap model (haiku) for tests
- Use TurnOutcome envelope (no SDK message-type imports)
- Assert against turn_record schema (unchanged) + post_message
  credit-pass results (new envelope)
- Tests are smaller (~30 LOC each) than SDK-tests because there's
  less mechanism to mock

The 3 SDK-specific test files (test_agent_sdk_client.py,
test_agent_saga.py, test_turn_hooks.py) become ~30 deepagents tests
covering the same ground. Net LOC: similar; complexity: lower.
"""
